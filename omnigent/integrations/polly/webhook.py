from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request

from omnigent.integrations.polly.store import PollyStore

_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


def create_webhook_app(
    store: PollyStore,
    webhook_secret: str,
    *,
    max_body_bytes: int = 1_048_576,
    repositories: tuple[str, ...] | None = None,
    ready: Callable[[], bool] | None = None,
) -> FastAPI:
    app = FastAPI()
    secret = webhook_secret.encode()
    allowed_repositories = (
        {repository.casefold() for repository in repositories}
        if repositories is not None
        else None
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        if ready is not None and not ready():
            raise HTTPException(503, "Polly worker unavailable")
        return {"status": "ok"}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> dict[str, str]:
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > max_body_bytes - len(body):
                raise HTTPException(413, "webhook body too large")
            body.extend(chunk)

        supplied = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret, bytes(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "invalid webhook signature")

        event = request.headers.get("X-GitHub-Event")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "webhook payload must be an object")

        if event != "pull_request" or payload.get("action") not in _ACTIONS:
            return {"status": "skipped"}
        pull = payload.get("pull_request")
        if not isinstance(pull, dict):
            raise HTTPException(400, "incomplete pull request payload")
        head = pull.get("head")
        base = pull.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise HTTPException(400, "incomplete pull request payload")
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            raise HTTPException(400, "incomplete pull request payload")
        if pull.get("draft") or head_repo.get("full_name") != base_repo.get("full_name"):
            return {"status": "skipped"}

        delivery = request.headers.get("X-GitHub-Delivery", "").strip()
        repo = payload.get("repository")
        installation = payload.get("installation")
        if not isinstance(repo, dict) or not isinstance(installation, dict):
            raise HTTPException(400, "incomplete pull request payload")
        try:
            owner = repo["owner"]["login"]
            name = repo["name"]
            number = int(pull["number"])
            head_sha = pull["head"]["sha"]
            installation_id = int(installation["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, "incomplete pull request payload") from exc
        if not delivery or not all(
            isinstance(value, str) and value for value in (owner, name, head_sha)
        ):
            raise HTTPException(400, "incomplete pull request payload")
        if (
            allowed_repositories is not None
            and f"{owner}/{name}".casefold() not in allowed_repositories
        ):
            return {"status": "skipped"}

        return {"status": store.accept(delivery, owner, name, number, head_sha, installation_id)}

    return app
