from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeAlias, cast

import httpx
import jwt

JsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # GitHub JSON boundary
PrivateKey: TypeAlias = Any  # type: ignore[explicit-any]  # PyJWT accepts crypto key objects
RequestKwargs: TypeAlias = Any  # type: ignore[explicit-any]  # forwarded to httpx
MAX_DIFF_BYTES = 400_000
_TRUNCATED = "\n\n[Diff truncated at 400,000 UTF-8 bytes. Review only the content provided.]\n"


class GitHubAppClient:
    def __init__(
        self,
        app_id: str,
        private_key: str | bytes | PrivateKey,
        http: httpx.AsyncClient,
        *,
        app_slug: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.http = http
        self.bot_login = f"{app_slug}[bot]"
        self.clock = clock
        self._installations: dict[tuple[str, str], int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}

    def app_jwt(self) -> str:
        issued = int(self.clock()) - 60
        return jwt.encode(
            {"iat": issued, "exp": issued + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    async def installation_for(self, owner: str, repo: str) -> int:
        key = (owner, repo)
        if key in self._installations:
            return self._installations[key]
        response = await self.http.get(
            f"/repos/{owner}/{repo}/installation",
            headers=self._app_headers(),
        )
        response.raise_for_status()
        installation_id = int(response.json()["id"])
        self._installations[key] = installation_id
        return installation_id

    def prime_installation(self, owner: str, repo: str, installation_id: int) -> None:
        self._installations[(owner, repo)] = installation_id

    async def installation_token(self, owner: str, repo: str) -> str:
        installation_id = await self.installation_for(owner, repo)
        return await self._installation_token(installation_id)

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - self.clock() > 300:
            return cached[0]
        response = await self.http.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=self._app_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        expires = payload.get("expires_at")
        try:
            parsed_expiry = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            parsed_expiry = self.clock() + 3600
        token = payload["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub installation token response carried no token")
        self._tokens[installation_id] = (token, parsed_expiry)
        return token

    async def get_pull(self, owner: str, repo: str, number: int) -> JsonObject:
        result = await self._json("GET", f"/repos/{owner}/{repo}/pulls/{number}", owner, repo)
        if not isinstance(result, dict):
            raise ValueError("GitHub pull response was not an object")
        return cast(JsonObject, result)

    async def get_diff(self, owner: str, repo: str, number: int) -> str:
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
            owner,
            repo,
            headers={"Accept": "application/vnd.github.diff"},
        )
        raw = response.content
        if len(raw) <= MAX_DIFF_BYTES:
            return raw.decode("utf-8")
        notice = _TRUNCATED.encode()
        prefix = raw[: MAX_DIFF_BYTES - len(notice)].decode("utf-8", errors="ignore")
        return prefix + _TRUNCATED

    async def find_review(
        self, owner: str, repo: str, number: int, marker: str
    ) -> JsonObject | None:
        page = 1
        while True:
            reviews = await self._json(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100&page={page}",
                owner,
                repo,
            )
            if not isinstance(reviews, list):
                raise ValueError("GitHub review list was not an array")
            found = next(
                (review for review in reviews if self._owned_marker(review, marker)),
                None,
            )
            if found or len(reviews) < 100:
                return found
            page += 1

    async def publish_review_once(
        self,
        owner: str,
        repo: str,
        number: int,
        head_sha: str,
        body: str,
        comments: list[JsonObject],
    ) -> JsonObject:
        marker = body.split("\n", 1)[0]
        existing = await self.find_review(owner, repo, number, marker)
        if existing:
            return existing
        payload = {
            "event": "COMMENT",
            "commit_id": head_sha,
            "body": body,
            "comments": [
                {
                    "path": comment["path"],
                    "line": comment["line"],
                    "side": "RIGHT",
                    "body": f"**{comment['priority']} · {comment['title']}**\n\n{comment['body']}",
                }
                for comment in comments
            ],
        }
        result = await self._json(
            "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", owner, repo, json=payload
        )
        if not isinstance(result, dict):
            raise ValueError("GitHub review response was not an object")
        return result

    async def upsert_status_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        cached_id: int | None = None,
    ) -> int:
        if cached_id is not None:
            response = await self._request(
                "PATCH",
                f"/repos/{owner}/{repo}/issues/comments/{cached_id}",
                owner,
                repo,
                json={"body": body},
                allowed={404},
            )
            if response.status_code != 404:
                return cached_id
        marker = body.split("\n", 1)[0]
        found = None
        page = 1
        while True:
            comments = await self._json(
                "GET",
                f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100&page={page}",
                owner,
                repo,
            )
            if not isinstance(comments, list):
                raise ValueError("GitHub comment list was not an array")
            found = next(
                (comment for comment in comments if self._owned_marker(comment, marker)),
                None,
            )
            if found or len(comments) < 100:
                break
            page += 1
        if found:
            await self._json(
                "PATCH",
                f"/repos/{owner}/{repo}/issues/comments/{found['id']}",
                owner,
                repo,
                json={"body": body},
            )
            return int(found["id"])
        created = await self._json(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            owner,
            repo,
            json={"body": body},
        )
        if not isinstance(created, dict):
            raise ValueError("GitHub comment response was not an object")
        return int(created["id"])

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self,
        method: str,
        path: str,
        owner: str,
        repo: str,
        *,
        allowed: set[int] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: RequestKwargs,
    ) -> httpx.Response:
        installation_id = await self.installation_for(owner, repo)
        token = await self._installation_token(installation_id)
        request_headers = self._base_headers(f"Bearer {token}")
        request_headers.update(headers or {})
        response = await self.http.request(method, path, headers=request_headers, **kwargs)
        if response.status_code == 401:
            self._tokens.pop(installation_id, None)
            token = await self._installation_token(installation_id)
            request_headers["Authorization"] = f"Bearer {token}"
            response = await self.http.request(method, path, headers=request_headers, **kwargs)
        if response.status_code not in (allowed or set()):
            response.raise_for_status()
        return response

    async def _json(
        self, method: str, path: str, owner: str, repo: str, **kwargs: RequestKwargs
    ) -> object:
        return cast(object, (await self._request(method, path, owner, repo, **kwargs)).json())

    @staticmethod
    def _base_headers(authorization: str) -> dict[str, str]:
        return {
            "Authorization": authorization,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _owned_marker(self, item: object, marker: str) -> bool:
        if not isinstance(item, dict):
            return False
        body = item.get("body")
        user = item.get("user")
        return (
            isinstance(body, str)
            and body.splitlines()[:1] == [marker]
            and isinstance(user, dict)
            and user.get("login") == self.bot_login
        )
