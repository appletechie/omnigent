from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias, cast

import httpx

from omnigent.integrations.polly.review import (
    ReviewValidationError,
    extract_review,
    review_schema,
)

REVIEWERS = ("claude_review", "codex_review")
JsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # REST/model JSON boundary
RequestKwargs: TypeAlias = Any  # type: ignore[explicit-any]  # forwarded to httpx


class ReviewerFailure(RuntimeError):
    pass


class OmnigentClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        access_token: str,
        refresh_token: str,
        device_client_secret: str = "",
        agent_id: str,
        host_id: str,
        workspace: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        save_tokens: Callable[[str, str], object | Awaitable[object]] | None = None,
        poll_interval: float = 1,
        max_polls: int = 600,
    ) -> None:
        self.http = http
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.device_client_secret = device_client_secret
        self.agent_id = agent_id
        self.host_id = host_id
        self.workspace = workspace
        self.sleep = sleep
        self.save_tokens = save_tokens
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self._refresh_lock = asyncio.Lock()

    async def review_pair(self, diff: str, review_id: str) -> dict[str, JsonObject]:
        started: list[str] = []
        tasks: list[asyncio.Task[JsonObject]] = []
        children: dict[str, str] = {}
        try:
            parent = await self._json(
                "POST",
                "/v1/sessions",
                json={
                    "agent_id": self.agent_id,
                    "title": f"Polly review {review_id}",
                    "host_id": self.host_id,
                    "workspace": self.workspace,
                    "subagent_routing_override": "off",
                },
            )
            parent_id = self._id(parent, "parent session")
            started.append(parent_id)
            uploaded = await self._json(
                "POST",
                f"/v1/sessions/{parent_id}/resources/files",
                files={"file": ("review.diff", diff.encode(), "text/plain")},
            )
            parent_file_id = self._id(uploaded, "uploaded diff")
            await self._wait_parent_ready(parent_id)
            schema = json.dumps(review_schema(review_id), separators=(",", ":"))

            for reviewer in REVIEWERS:
                child = await self._json(
                    "POST",
                    "/v1/sessions",
                    json={
                        "agent_id": self.agent_id,
                        "parent_session_id": parent_id,
                        "sub_agent_name": reviewer,
                        "title": f"{reviewer}:{review_id}",
                        "subagent_routing_override": "off",
                    },
                )
                child_id = self._id(child, f"{reviewer} child session")
                started.append(child_id)
                copied = await self._json(
                    "POST",
                    f"/v1/sessions/{child_id}/resources/files:copy",
                    json={"source_session_id": parent_id, "file_ids": [parent_file_id]},
                )
                try:
                    child_file_id = copied["mapping"][parent_file_id]["new_id"]
                except (KeyError, TypeError) as exc:
                    raise ReviewerFailure(f"{reviewer} diff copy returned no file id") from exc
                await self._request(
                    "POST",
                    f"/v1/sessions/{child_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        f"Review the attached diff. Review-ID: {review_id}. "
                                        "Return only one JSON object matching this exact schema: "
                                        f"{schema}"
                                    ),
                                },
                                {
                                    "type": "input_file",
                                    "file_id": child_file_id,
                                    "filename": "review.diff",
                                },
                            ],
                        },
                    },
                )
                children[reviewer] = child_id

            tasks = [
                asyncio.create_task(self._poll(children[reviewer], review_id))
                for reviewer in REVIEWERS
            ]
            results = await asyncio.gather(*tasks)
            return dict(zip(REVIEWERS, results, strict=True))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                *(self.interrupt(session_id) for session_id in started),
                return_exceptions=True,
            )
            raise

    async def interrupt(self, session_id: str) -> None:
        try:
            await self._request(
                "POST", f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}
            )
        except httpx.HTTPError:
            return

    async def _poll(self, session_id: str, review_id: str) -> JsonObject:
        observed_active = False
        for _ in range(self.max_polls):
            snapshot = await self._json("GET", f"/v1/sessions/{session_id}")
            status = snapshot.get("status")
            if status in {"failed", "error", "cancelled"}:
                raise ReviewerFailure(f"reviewer session {session_id} {status}")
            if status in {"running", "waiting"}:
                observed_active = True
            if status in {"idle", "completed"}:
                text = self._assistant_text(snapshot)
                if text is not None:
                    try:
                        return extract_review(text, review_id)
                    except ReviewValidationError as exc:
                        raise ReviewerFailure(str(exc)) from exc
                if status == "completed" or observed_active:
                    raise ReviewerFailure(
                        f"reviewer session {session_id} completed without output"
                    )
            await self.sleep(self.poll_interval)
        raise ReviewerFailure(f"reviewer session {session_id} did not finish")

    async def _wait_parent_ready(self, session_id: str) -> None:
        for _ in range(self.max_polls):
            snapshot = await self._json("GET", f"/v1/sessions/{session_id}")
            if snapshot.get("runner_online") is True and snapshot.get("runner_id"):
                return
            if snapshot.get("status") == "failed":
                raise ReviewerFailure("parent session runner failed to start")
            await self.sleep(self.poll_interval)
        raise ReviewerFailure("parent session runner did not become ready")

    async def _request(self, method: str, path: str, **kwargs: RequestKwargs) -> httpx.Response:
        token = self.access_token
        response = await self.http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 401:
            await self._refresh(token)
            response = await self.http.request(
                method,
                path,
                headers={"Authorization": f"Bearer {self.access_token}"},
                **kwargs,
            )
        response.raise_for_status()
        return response

    async def _refresh(self, failed_token: str) -> None:
        async with self._refresh_lock:
            if self.access_token != failed_token:
                return
            response = await self.http.post(
                "/oauth/token",
                headers=(
                    {"X-Omnigent-Client-Secret": self.device_client_secret}
                    if self.device_client_secret
                    else {}
                ),
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            )
            response.raise_for_status()
            payload = response.json()
            self.access_token = payload["access_token"]
            self.refresh_token = payload["refresh_token"]
            if self.save_tokens:
                saved = self.save_tokens(self.access_token, self.refresh_token)
                if inspect.isawaitable(saved):
                    await saved

    async def _json(self, method: str, path: str, **kwargs: RequestKwargs) -> JsonObject:
        payload = cast(object, (await self._request(method, path, **kwargs)).json())
        if not isinstance(payload, dict):
            raise ReviewerFailure(f"{path} returned a non-object response")
        return cast(JsonObject, payload)

    @staticmethod
    def _id(payload: object, label: str) -> str:
        value = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise ReviewerFailure(f"{label} returned no id")
        return value

    @staticmethod
    def _assistant_text(snapshot: JsonObject) -> str | None:
        items = snapshot.get("items") or snapshot.get("history") or []
        texts: list[str] = []
        for item in items:
            data = item.get("data", item) if isinstance(item, dict) else {}
            if data.get("role") != "assistant":
                continue
            content = data.get("content", [])
            if isinstance(content, str):
                texts.append(content)
                continue
            parts: list[str] = []
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                texts.append("".join(parts))
        return texts[-1] if texts else None
