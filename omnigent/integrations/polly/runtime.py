from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import uvicorn
from fastapi import FastAPI

from omnigent.integration_daemon import IntegrationDaemon
from omnigent.integrations.polly.cli import (
    GITHUB_PRIVATE_KEY_SECRET,
    OMNIGENT_ACCESS_TOKEN_SECRET,
    OMNIGENT_DEVICE_CLIENT_SECRET,
    OMNIGENT_REFRESH_TOKEN_SECRET,
    WEBHOOK_SECRET_SECRET,
    PollyConfig,
    database_path,
    integration_dir,
    load_config,
)
from omnigent.integrations.polly.github import GitHubAppClient
from omnigent.integrations.polly.omnigent import OmnigentClient
from omnigent.integrations.polly.store import PollyStore
from omnigent.integrations.polly.webhook import create_webhook_app
from omnigent.integrations.polly.worker import PollyWorker
from omnigent.onboarding import secrets as secret_store

logger = logging.getLogger(__name__)


@dataclass
class PollyRuntime:
    app: FastAPI
    github_http: httpx.AsyncClient
    omnigent_http: httpx.AsyncClient
    started: asyncio.Event


def build_runtime(
    *,
    config: PollyConfig,
    private_key: str,
    webhook_secret: str,
    refresh_token: str,
    access_token: str = "",
    device_client_secret: str = "",
    store: PollyStore | None = None,
    daemon: IntegrationDaemon | None = None,
    worker: PollyWorker | None = None,
    idle_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PollyRuntime:
    store = store or PollyStore(database_path())
    daemon = daemon or IntegrationDaemon("polly", integration_dir().parents[1])
    github_http = httpx.AsyncClient(base_url="https://api.github.com", timeout=30)
    omnigent_http = httpx.AsyncClient(base_url=config.server_url, timeout=30)
    github = GitHubAppClient(
        config.github_app_id,
        private_key,
        github_http,
        app_slug=config.github_app_slug,
    )

    def save_tokens(access: str, refresh: str) -> None:
        secret_store.store_secret(OMNIGENT_REFRESH_TOKEN_SECRET, refresh)
        secret_store.store_secret(OMNIGENT_ACCESS_TOKEN_SECRET, access)

    omnigent = OmnigentClient(
        omnigent_http,
        access_token=access_token,
        refresh_token=refresh_token,
        device_client_secret=device_client_secret,
        agent_id=config.agent_id,
        host_id=config.host_id,
        workspace=config.workspace,
        save_tokens=save_tokens,
    )
    worker = worker or PollyWorker(store, github, omnigent)
    started = asyncio.Event()
    stop = asyncio.Event()
    task: asyncio.Task[None] | None = None

    def worker_ready() -> bool:
        return task is not None and not task.done()

    app = create_webhook_app(
        store, webhook_secret, repositories=config.repositories, ready=worker_ready
    )

    async def work() -> None:
        while not stop.is_set():
            try:
                did_work = await worker.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Polly worker iteration failed")
                await idle_sleep(0.5)
                continue
            if not did_work:
                await idle_sleep(0.5)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal task
        daemon.acquire_current(integration_dir() / "polly.log")
        store.recover_running()
        task = asyncio.create_task(work())
        started.set()
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await github_http.aclose()
            await omnigent_http.aclose()
            daemon.release_current()

    app.router.lifespan_context = lifespan
    return PollyRuntime(
        app=app,
        github_http=github_http,
        omnigent_http=omnigent_http,
        started=started,
    )


def _required_secret(name: str) -> str:
    value = secret_store.load_secret(name)
    if not value or not value.strip():
        raise RuntimeError(f"missing Polly secret slot: {name}")
    return value


def _optional_secret(name: str) -> str:
    return secret_store.load_secret(name) or ""


def main() -> None:
    config = load_config()
    runtime = build_runtime(
        config=config,
        private_key=_required_secret(GITHUB_PRIVATE_KEY_SECRET),
        webhook_secret=_required_secret(WEBHOOK_SECRET_SECRET),
        refresh_token=_required_secret(OMNIGENT_REFRESH_TOKEN_SECRET),
        access_token=_optional_secret(OMNIGENT_ACCESS_TOKEN_SECRET),
        device_client_secret=_optional_secret(OMNIGENT_DEVICE_CLIENT_SECRET),
    )
    uvicorn.run(runtime.app, host=config.listen_host, port=config.listen_port)


if __name__ == "__main__":
    main()
