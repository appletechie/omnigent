from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from unittest import mock

import httpx
import pytest

from omnigent.integrations.polly.cli import PollyConfig
from omnigent.integrations.polly.runtime import _required_secret, build_runtime
from omnigent.integrations.polly.store import PollyStore
from omnigent.onboarding import secrets


def test_runtime_required_secret_rejects_blank_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    secrets.store_secret("polly-github-webhook-secret", " \n")

    with pytest.raises(RuntimeError, match="missing Polly secret slot"):
        _required_secret("polly-github-webhook-secret")


@pytest.mark.asyncio
async def test_runtime_acquires_singleton_recovers_once_and_cleans_up(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PollyConfig(
        server_url="http://127.0.0.1:8000",
        public_url="https://polly.example.com",
        listen_host="127.0.0.1",
        listen_port=8788,
        workspace=str(workspace),
        agent_id="ag_polly",
        host_id="host_1",
        github_app_id="123",
        github_app_slug="polly-test",
        repositories=("acme/widgets",),
    )
    store = mock.Mock()
    store.recover_running.return_value = 1
    daemon = mock.Mock()
    worker = mock.Mock()
    worker.run_once = mock.AsyncMock(side_effect=[False])

    runtime = build_runtime(
        config=config,
        private_key="private",
        webhook_secret="webhook",
        refresh_token="refresh",
        access_token="access",
        store=store,
        daemon=daemon,
        worker=worker,
        idle_sleep=mock.AsyncMock(),
    )
    async with runtime.app.router.lifespan_context(runtime.app):
        await runtime.started.wait()

    daemon.acquire_current.assert_called_once()
    store.recover_running.assert_called_once_with()
    daemon.release_current.assert_called_once()
    await runtime.github_http.aclose()
    await runtime.omnigent_http.aclose()


@pytest.mark.asyncio
async def test_runtime_continues_after_one_worker_iteration_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PollyConfig(
        server_url="http://127.0.0.1:8000",
        public_url="https://polly.example.com",
        listen_host="127.0.0.1",
        listen_port=8788,
        workspace=str(workspace),
        agent_id="ag_polly",
        host_id="host_1",
        github_app_id="123",
        github_app_slug="polly-test",
        repositories=("acme/widgets",),
    )
    progressed = asyncio.Event()
    calls = 0

    async def run_once() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary store failure")
        progressed.set()
        return False

    async def idle_sleep(_: float) -> None:
        await asyncio.sleep(0)

    worker = mock.Mock()
    worker.run_once = run_once
    runtime = build_runtime(
        config=config,
        private_key="private",
        webhook_secret="webhook",
        refresh_token="refresh",
        daemon=mock.Mock(),
        worker=worker,
        idle_sleep=idle_sleep,
    )

    with caplog.at_level("ERROR"):
        async with runtime.app.router.lifespan_context(runtime.app):
            await asyncio.wait_for(progressed.wait(), timeout=1)

    assert calls >= 2
    assert "Polly worker iteration failed" in caplog.text
    await runtime.github_http.aclose()
    await runtime.omnigent_http.aclose()


@pytest.mark.asyncio
async def test_runtime_health_is_unavailable_when_worker_task_stops(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PollyConfig(
        server_url="http://127.0.0.1:8000",
        public_url="https://polly.example.com",
        listen_host="127.0.0.1",
        listen_port=8788,
        workspace=str(workspace),
        agent_id="ag_polly",
        host_id="host_1",
        github_app_id="123",
        github_app_slug="polly-test",
        repositories=("acme/widgets",),
    )
    started = asyncio.Event()

    async def run_once() -> bool:
        started.set()
        raise asyncio.CancelledError

    worker = mock.Mock()
    worker.run_once = run_once
    runtime = build_runtime(
        config=config,
        private_key="private",
        webhook_secret="webhook",
        refresh_token="refresh",
        daemon=mock.Mock(),
        worker=worker,
    )

    async with runtime.app.router.lifespan_context(runtime.app):
        await started.wait()
        await asyncio.sleep(0)
        transport = httpx.ASGITransport(app=runtime.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 503
    await runtime.github_http.aclose()
    await runtime.omnigent_http.aclose()


@pytest.mark.asyncio
async def test_runtime_persists_rotated_access_and_refresh_tokens(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PollyConfig(
        server_url="http://127.0.0.1:8000",
        public_url="https://polly.example.com",
        listen_host="127.0.0.1",
        listen_port=8788,
        workspace=str(workspace),
        agent_id="ag_polly",
        host_id="host_1",
        github_app_id="123",
        github_app_slug="polly-test",
        repositories=("acme/widgets",),
    )
    worker = mock.Mock()
    worker.run_once = mock.AsyncMock(return_value=False)
    with (
        mock.patch("omnigent.integrations.polly.runtime.OmnigentClient") as client,
        mock.patch("omnigent.integrations.polly.runtime.secret_store.store_secret") as store,
    ):
        runtime = build_runtime(
            config=config,
            private_key="private",
            webhook_secret="webhook",
            refresh_token="refresh",
            access_token="access",
            device_client_secret="client-secret",
            daemon=mock.Mock(),
            worker=worker,
            idle_sleep=mock.AsyncMock(),
        )
        client.call_args.kwargs["save_tokens"]("new-access", "new-refresh")

    assert [call.args for call in store.call_args_list] == [
        ("polly-omnigent-refresh-token", "new-refresh"),
        ("polly-omnigent-access-token", "new-access"),
    ]
    await runtime.github_http.aclose()
    await runtime.omnigent_http.aclose()


@pytest.mark.asyncio
async def test_runtime_passes_configured_repository_allowlist_to_webhook(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PollyConfig(
        server_url="http://127.0.0.1:8000",
        public_url="https://polly.example.com",
        listen_host="127.0.0.1",
        listen_port=8788,
        workspace=str(workspace),
        agent_id="ag_polly",
        host_id="host_1",
        github_app_id="123",
        github_app_slug="polly-test",
        repositories=("acme/widgets",),
    )
    store = PollyStore(tmp_path / "polly.sqlite3")
    runtime = build_runtime(
        config=config,
        private_key="private",
        webhook_secret="secret",
        refresh_token="refresh",
        store=store,
        daemon=mock.Mock(),
        worker=mock.Mock(),
    )
    payload = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 7},
            "repository": {
                "full_name": "acme/other",
                "owner": {"login": "acme"},
                "name": "other",
            },
            "pull_request": {
                "number": 12,
                "draft": False,
                "head": {
                    "sha": "head-1",
                    "repo": {"full_name": "acme/other"},
                },
                "base": {"repo": {"full_name": "acme/other"}},
            },
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=runtime.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/github",
            content=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": signature,
            },
        )

    assert response.json() == {"status": "skipped"}
    assert store.list_jobs() == []
    await runtime.github_http.aclose()
    await runtime.omnigent_http.aclose()
