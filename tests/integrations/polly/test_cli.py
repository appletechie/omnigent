from __future__ import annotations

from pathlib import Path
from unittest import mock

import click
import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.integration_daemon import DaemonRecord, IntegrationDaemon
from omnigent.integrations.polly.cli import (
    GITHUB_PRIVATE_KEY_SECRET,
    OMNIGENT_ACCESS_TOKEN_SECRET,
    OMNIGENT_DEVICE_CLIENT_SECRET,
    OMNIGENT_REFRESH_TOKEN_SECRET,
    WEBHOOK_SECRET_SECRET,
    PollyConfig,
    _manifest_registration_url,
    _run_setup,
    _validate_repository_installations,
    _validate_server_selection,
    acquire_device_tokens,
    exchange_manifest_code,
    load_config,
    run_doctor,
    save_config,
    validate_public_url,
    validate_server_url,
    validate_workspace,
)
from omnigent.integrations.polly.store import PollyStore
from omnigent.onboarding import secrets


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    return tmp_path


def _config(root: Path) -> PollyConfig:
    workspace = root / "workspace"
    workspace.mkdir()
    return PollyConfig(
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


def test_config_is_non_secret_and_uses_stable_secret_slots(isolated: Path) -> None:
    cfg = _config(isolated)
    save_config(cfg)
    secrets.store_secret(GITHUB_PRIVATE_KEY_SECRET, "PRIVATE-KEY")
    secrets.store_secret(WEBHOOK_SECRET_SECRET, "WEBHOOK-SECRET")
    secrets.store_secret(OMNIGENT_REFRESH_TOKEN_SECRET, "REFRESH-TOKEN")
    secrets.store_secret(OMNIGENT_ACCESS_TOKEN_SECRET, "ACCESS-TOKEN")
    secrets.store_secret(OMNIGENT_DEVICE_CLIENT_SECRET, "CLIENT-SECRET")

    text = (isolated / "integrations" / "polly" / "config.json").read_text()

    assert load_config() == cfg
    assert "PRIVATE-KEY" not in text
    assert "WEBHOOK-SECRET" not in text
    assert "REFRESH-TOKEN" not in text
    assert "ACCESS-TOKEN" not in text
    assert "CLIENT-SECRET" not in text


def test_device_authorization_sends_optional_client_secret_on_every_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/device/authorize":
            return httpx.Response(
                200,
                json={
                    "verification_uri_complete": "https://omni.test/device?code=ABCD",
                    "device_code": "device",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://omni.test"
        ) as client,
        mock.patch("omnigent.integrations.polly.cli.webbrowser.open"),
    ):
        tokens = acquire_device_tokens(
            "https://omni.test", client=client, device_client_secret="client-secret"
        )

    assert tokens == ("access", "refresh")
    assert len(requests) == 2
    assert all(
        request.headers["X-Omnigent-Client-Secret"] == "client-secret" for request in requests
    )


@pytest.mark.parametrize(
    "status,payload",
    [
        (404, {}),
        (200, {"access_token": "", "refresh_token": "refresh"}),
        (200, {"access_token": "access", "refresh_token": "   "}),
        (200, {"access_token": 1, "refresh_token": "refresh"}),
        (200, {"access_token": "access"}),
    ],
)
def test_device_authorization_rejects_unscoped_or_invalid_tokens(
    status: int, payload: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/device/authorize":
            if status == 404:
                return httpx.Response(status, json=payload)
            return httpx.Response(
                200,
                json={
                    "verification_uri_complete": "https://omni.test/device?code=ABCD",
                    "device_code": "device",
                    "expires_in": 60,
                    "interval": 1,
                },
            )
        return httpx.Response(status, json=payload)

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8000"
        ) as client,
        mock.patch("omnigent.integrations.polly.cli.webbrowser.open"),
        pytest.raises(click.ClickException),
    ):
        acquire_device_tokens("http://127.0.0.1:8000", client=client)


@pytest.mark.parametrize("url", ["http://example.com", "https://", "ftp://x.test"])
def test_public_url_requires_https(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_public_url_allows_loopback_http_only_when_explicit() -> None:
    with pytest.raises(ValueError):
        validate_public_url("http://127.0.0.1:8788")
    assert validate_public_url("http://127.0.0.1:8788", allow_loopback_http=True) == (
        "http://127.0.0.1:8788"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com",
        "https://example.com?token=secret",
        "https://example.com#fragment",
        "https://example.com:invalid",
        "https://example.com//",
    ],
)
def test_public_url_requires_a_strict_origin(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_server_url_allows_https_or_loopback_http_only() -> None:
    assert validate_server_url("https://omnigent.example.com/") == "https://omnigent.example.com"
    assert validate_server_url("http://localhost:8000/") == "http://localhost:8000"


@pytest.mark.parametrize(
    "url",
    [
        "http://omnigent.example.com",
        "ftp://omnigent.example.com",
        "https://",
        "https://user:password@omnigent.example.com",
        "https://omnigent.example.com/v1",
        "https://omnigent.example.com?token=secret",
        "https://omnigent.example.com#fragment",
        "https://omnigent.example.com:invalid",
    ],
)
def test_server_url_requires_a_secure_strict_origin(url: str) -> None:
    with pytest.raises(ValueError):
        validate_server_url(url)


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/",
        "/root",
        "/root/",
        "/root/.",
        "/root/../root",
        "/home/alice",
        "/home/alice/",
        "/home/alice/../alice",
        "/Users/alice",
        "/Users/alice/.",
    ],
)
def test_workspace_rejects_non_dedicated_paths(path: str) -> None:
    with pytest.raises(ValueError):
        validate_workspace(path)


def test_workspace_returns_a_canonical_absolute_path() -> None:
    assert validate_workspace("/srv//polly/") == "/srv/polly"


def test_manifest_exchange_checks_state_and_exchanges_code_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 123,
                "slug": "polly-test",
                "pem": "PRIVATE-KEY",
                "webhook_secret": "WEBHOOK-SECRET",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = exchange_manifest_code(
            "http://127.0.0.1/callback?state=expected&code=once",
            expected_state="expected",
            client=client,
        )

    assert result["slug"] == "polly-test"
    assert requests[0].url.path == "/app-manifests/once/conversions"
    with pytest.raises(ValueError, match="state"):
        exchange_manifest_code(
            "http://127.0.0.1/callback?state=wrong&code=once",
            expected_state="expected",
            client=mock.Mock(),
        )


def test_manifest_registration_can_target_an_organization() -> None:
    assert _manifest_registration_url(None) == "https://github.com/settings/apps/new"
    assert _manifest_registration_url("acme") == (
        "https://github.com/organizations/acme/settings/apps/new"
    )


def test_server_selection_accepts_explicit_ready_host_and_lists_ambiguous_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [{"id": "ag_review", "name": "polly-review"}]}
            )
        if request.url.path == "/v1/hosts":
            return httpx.Response(
                200,
                json={
                    "hosts": [
                        {
                            "host_id": "host_1",
                            "name": "laptop",
                            "status": "online",
                            "configured_harnesses": {"claude-sdk": True, "codex": True},
                        },
                        {
                            "host_id": "host_2",
                            "name": "server",
                            "status": "online",
                            "configured_harnesses": {"claude-sdk": True, "codex": True},
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"data": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        "omnigent.integrations.polly.cli.httpx.Client",
        lambda *args, **kwargs: real_client(
            base_url=kwargs.get("base_url"),
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(handler),
        ),
    )

    assert _validate_server_selection("https://omni.test", "token", "/srv/polly", "server") == (
        "ag_review",
        "host_2",
    )
    assert requests[0].url.params["limit"] == "1000"

    with pytest.raises(click.ClickException) as exc_info:
        _validate_server_selection("https://omni.test", "token", "/srv/polly", None)
    message = str(exc_info.value)
    assert "laptop (host_1)" in message
    assert "server (host_2)" in message


def test_setup_host_option_is_forwarded(isolated: Path) -> None:
    with mock.patch("omnigent.integrations.polly.cli._run_setup") as run_setup:
        result = CliRunner().invoke(
            cli,
            [
                "integration",
                "polly",
                "setup",
                "--server",
                "http://127.0.0.1:8000",
                "--public-url",
                "https://polly.example.com",
                "--workspace",
                str(isolated / "workspace"),
                "--host",
                "laptop",
            ],
        )

    assert result.exit_code == 0, result.output
    assert run_setup.call_args.kwargs["host"] == "laptop"


def test_manifest_setup_opens_install_url_and_validates_selected_repositories(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = isolated / "workspace"
    workspace.mkdir()
    app = {
        "id": 123,
        "slug": "polly-test",
        "pem": "PRIVATE-KEY",
        "webhook_secret": "WEBHOOK-SECRET",
    }
    with (
        mock.patch(
            "omnigent.integrations.polly.cli.acquire_device_tokens",
            return_value=("access", "refresh"),
        ),
        mock.patch(
            "omnigent.integrations.polly.cli._validate_server_selection",
            return_value=("ag_review", "host_1"),
        ),
        mock.patch("omnigent.integrations.polly.cli._manifest_flow", return_value=app),
        mock.patch(
            "omnigent.integrations.polly.cli._validate_repository_installations"
        ) as validate,
        mock.patch("omnigent.integrations.polly.cli.webbrowser.open") as open_browser,
        mock.patch("omnigent.integrations.polly.cli.secret_store.store_secret"),
        mock.patch("omnigent.integrations.polly.cli.save_config"),
    ):
        _run_setup(
            server="http://127.0.0.1:8000",
            public_url="https://polly.example.com",
            listen_host="127.0.0.1",
            listen_port=8788,
            workspace=str(workspace),
            host=None,
            app_id=None,
            app_slug=None,
            private_key_file=None,
            repositories=("acme/widgets",),
            allow_loopback_http=False,
        )

    install_url = "https://github.com/apps/polly-test/installations/new"
    open_browser.assert_called_once_with(install_url)
    assert install_url in capsys.readouterr().out
    validate.assert_called_once_with("123", "PRIVATE-KEY", ("acme/widgets",))


def test_existing_app_repository_validation_failure_writes_no_config_or_secrets(
    isolated: Path,
) -> None:
    workspace = isolated / "workspace"
    workspace.mkdir()
    key = isolated / "app.pem"
    key.write_text("PRIVATE-KEY")
    with (
        mock.patch(
            "omnigent.integrations.polly.cli.acquire_device_tokens",
            return_value=("access", "refresh"),
        ),
        mock.patch(
            "omnigent.integrations.polly.cli._validate_server_selection",
            return_value=("ag_review", "host_1"),
        ),
        mock.patch("omnigent.integrations.polly.cli._validate_existing_app"),
        mock.patch("omnigent.integrations.polly.cli.click.prompt", return_value="WEBHOOK-SECRET"),
        mock.patch(
            "omnigent.integrations.polly.cli._validate_repository_installations",
            side_effect=click.ClickException("not installed on acme/widgets"),
        ),
        mock.patch("omnigent.integrations.polly.cli.secret_store.store_secret") as store_secret,
        mock.patch("omnigent.integrations.polly.cli.save_config") as save,
    ):
        with pytest.raises(click.ClickException, match="not installed"):
            _run_setup(
                server="http://127.0.0.1:8000",
                public_url="https://polly.example.com",
                listen_host="127.0.0.1",
                listen_port=8788,
                workspace=str(workspace),
                host=None,
                app_id="123",
                app_slug="polly-test",
                private_key_file=key,
                repositories=("acme/widgets",),
                allow_loopback_http=False,
            )

    store_secret.assert_not_called()
    save.assert_not_called()


@pytest.mark.parametrize(
    "private_key,webhook_secret", [("   ", "WEBHOOK-SECRET"), ("PRIVATE-KEY", "  ")]
)
def test_existing_app_blank_required_input_writes_no_config_or_secrets(
    isolated: Path, private_key: str, webhook_secret: str
) -> None:
    workspace = isolated / "workspace"
    workspace.mkdir()
    key = isolated / "app.pem"
    key.write_text(private_key)
    with (
        mock.patch(
            "omnigent.integrations.polly.cli.acquire_device_tokens",
            return_value=("access", "refresh"),
        ),
        mock.patch(
            "omnigent.integrations.polly.cli._validate_server_selection",
            return_value=("ag_review", "host_1"),
        ),
        mock.patch("omnigent.integrations.polly.cli.click.prompt", return_value=webhook_secret),
        mock.patch("omnigent.integrations.polly.cli._validate_existing_app") as validate_app,
        mock.patch("omnigent.integrations.polly.cli.secret_store.store_secret") as store_secret,
        mock.patch("omnigent.integrations.polly.cli.save_config") as save,
    ):
        with pytest.raises(click.ClickException, match="must not be empty"):
            _run_setup(
                server="http://127.0.0.1:8000",
                public_url="https://polly.example.com",
                listen_host="127.0.0.1",
                listen_port=8788,
                workspace=str(workspace),
                host=None,
                app_id="123",
                app_slug="polly-test",
                private_key_file=key,
                repositories=("acme/widgets",),
                allow_loopback_http=False,
            )

    validate_app.assert_not_called()
    store_secret.assert_not_called()
    save.assert_not_called()


def test_repository_installation_validation_rejects_uninstalled_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr("jwt.encode", lambda *args, **kwargs: "app-jwt")
    monkeypatch.setattr(
        "omnigent.integrations.polly.cli.httpx.Client",
        lambda *args, **kwargs: real_client(
            base_url=kwargs.get("base_url"),
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(click.ClickException, match="acme/missing"):
        _validate_repository_installations("123", "PRIVATE-KEY", ("acme/widgets", "acme/missing"))


def test_cli_lifecycle_uses_polly_daemon(isolated: Path) -> None:
    save_config(_config(isolated))
    runner = CliRunner()
    with (
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch.object(IntegrationDaemon, "confirm_alive", return_value=True),
    ):
        popen.return_value.pid = 9912
        started = runner.invoke(cli, ["integration", "polly", "--background"])
        assert started.exit_code == 0, started.output
        assert popen.call_args.args[0][1:] == ["-m", "omnigent.integrations.polly.runtime"]
        assert "9912" in runner.invoke(cli, ["integration", "polly", "status"]).output

    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=[True, False, False]),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        stopped = runner.invoke(cli, ["integration", "polly", "stop"])
    assert stopped.exit_code == 0
    assert "Stopped" in stopped.output


def test_cli_foreground_and_logs_follow(isolated: Path) -> None:
    save_config(_config(isolated))
    runner = CliRunner()
    with mock.patch("omnigent.integrations.polly.cli.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0)
        result = runner.invoke(cli, ["integration", "polly"])
    assert result.exit_code == 0, result.output
    assert run.call_args.args[0][1:] == ["-m", "omnigent.integrations.polly.runtime"]

    log = isolated / "polly.log"
    log.write_text("safe log\n")
    IntegrationDaemon("polly", isolated)._write_record(
        DaemonRecord(pid=1, log_path=str(log), started_at=1)
    )
    with mock.patch("omnigent.integrations.polly.cli.subprocess.run") as run:
        followed = runner.invoke(cli, ["integration", "polly", "logs", "-f"])
    assert followed.exit_code == 0
    assert run.call_args.args[0] == ["tail", "-f", str(log)]


def test_retry_only_resets_failed_job(isolated: Path) -> None:
    save_config(_config(isolated))
    store = PollyStore(isolated / "integrations" / "polly" / "polly.sqlite3")
    store.accept("d1", "acme", "widgets", 1, "head", 7)
    job = store.list_jobs()[0]
    runner = CliRunner()

    refused = runner.invoke(cli, ["integration", "polly", "retry", str(job.id)])
    assert refused.exit_code != 0
    store.set_state(job.id, "failed", error="boom")
    retried = runner.invoke(cli, ["integration", "polly", "retry", str(job.id)])

    assert retried.exit_code == 0, retried.output
    assert store.get_job(job.id).state == "pending"


def test_jobs_lists_failed_id_without_secret_values(isolated: Path) -> None:
    save_config(_config(isolated))
    secrets.store_secret(OMNIGENT_ACCESS_TOKEN_SECRET, "ACCESS-TOKEN")
    store = PollyStore(isolated / "integrations" / "polly" / "polly.sqlite3")
    store.accept("d1", "acme", "widgets", 1, "head", 7)
    job = store.list_jobs()[0]
    store.set_state(job.id, "failed", error="GitHub request failed after retry: ACCESS-TOKEN")

    result = CliRunner().invoke(cli, ["integration", "polly", "jobs"])

    assert result.exit_code == 0, result.output
    assert f"{job.id}" in result.output
    assert "acme/widgets" in result.output
    assert "#1" in result.output
    assert "head" in result.output
    assert "failed" in result.output
    assert "GitHub request failed after retry: [redacted]" in result.output
    assert "ACCESS-TOKEN" not in result.output


def test_jobs_redacts_multiline_and_cutoff_secret_before_summary(isolated: Path) -> None:
    save_config(_config(isolated))
    multiline_secret = "MULTILINE-SECRET\nSECOND-LINE"
    cutoff_secret = "CUTOFF-SECRET-MARKER"
    secrets.store_secret(GITHUB_PRIVATE_KEY_SECRET, multiline_secret)
    secrets.store_secret(WEBHOOK_SECRET_SECRET, cutoff_secret)
    store = PollyStore(isolated / "integrations" / "polly" / "polly.sqlite3")
    store.accept("d1", "acme", "widgets", 1, "head-1", 7)
    store.accept("d2", "acme", "widgets", 2, "head-2", 7)
    first, second = store.list_jobs()
    store.set_state(first.id, "failed", error=f"request failed: {multiline_secret}")
    store.set_state(second.id, "failed", error=("x" * 195) + cutoff_secret)

    result = CliRunner().invoke(cli, ["integration", "polly", "jobs"])

    assert result.exit_code == 0, result.output
    assert "MULTILINE-SECRET" not in result.output
    assert "SECOND-LINE" not in result.output
    assert "MULTILINE-SECRET SECOND-LINE" not in result.output
    assert "CUTOF" not in result.output


def test_doctor_rejects_blank_required_secret_slots(isolated: Path) -> None:
    save_config(_config(isolated))
    for name in (
        GITHUB_PRIVATE_KEY_SECRET,
        WEBHOOK_SECRET_SECRET,
        OMNIGENT_REFRESH_TOKEN_SECRET,
        OMNIGENT_ACCESS_TOKEN_SECRET,
    ):
        secrets.store_secret(name, "   ")

    checks = run_doctor()

    assert checks["secrets"] is False
    assert checks["access token"] is False


def test_setup_refuses_running_daemon_before_reading_secrets(isolated: Path) -> None:
    IntegrationDaemon("polly", isolated)._write_record(
        DaemonRecord(pid=77, log_path=str(isolated / "log"), started_at=1)
    )
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch("omnigent.integrations.polly.cli._run_setup") as setup,
    ):
        result = CliRunner().invoke(
            cli,
            [
                "integration",
                "polly",
                "setup",
                "--server",
                "http://127.0.0.1:8000",
                "--public-url",
                "https://polly.example.com",
                "--workspace",
                str(isolated / "workspace"),
            ],
        )
    assert result.exit_code != 0
    assert "running" in result.output.lower()
    setup.assert_not_called()


def test_doctor_reports_each_read_only_check_without_secret_values(isolated: Path) -> None:
    save_config(_config(isolated))
    for name, value in (
        (GITHUB_PRIVATE_KEY_SECRET, "PRIVATE-KEY"),
        (WEBHOOK_SECRET_SECRET, "WEBHOOK-SECRET"),
        (OMNIGENT_REFRESH_TOKEN_SECRET, "REFRESH-TOKEN"),
        (OMNIGENT_ACCESS_TOKEN_SECRET, "ACCESS-TOKEN"),
    ):
        secrets.store_secret(name, value)
    checks = {
        "config": True,
        "secrets": True,
        "access token": True,
        "agent": True,
        "host": True,
        "harness readiness": True,
        "workspace": False,
        "GitHub App": True,
        "installation repositories": True,
        "public health": True,
    }
    with mock.patch("omnigent.integrations.polly.cli.run_doctor", return_value=checks):
        result = CliRunner().invoke(cli, ["integration", "polly", "doctor"])
    assert result.exit_code != 0
    assert "workspace: FAIL" in result.output
    assert all(
        value not in result.output
        for value in ("PRIVATE-KEY", "WEBHOOK-SECRET", "REFRESH-TOKEN", "ACCESS-TOKEN")
    )


def test_doctor_does_not_rotate_or_write_credentials(isolated: Path) -> None:
    config = _config(isolated)
    save_config(config)
    for name, value in (
        (GITHUB_PRIVATE_KEY_SECRET, "PRIVATE-KEY"),
        (WEBHOOK_SECRET_SECRET, "WEBHOOK-SECRET"),
        (OMNIGENT_REFRESH_TOKEN_SECRET, "REFRESH-TOKEN"),
        (OMNIGENT_ACCESS_TOKEN_SECRET, "ACCESS-TOKEN"),
    ):
        secrets.store_secret(name, value)

    def response(url: str, payload: object) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    server_client = mock.MagicMock()
    server_client.__enter__.return_value = server_client
    server_client.__exit__.return_value = None
    server_client.get.side_effect = [
        response(
            "https://omni.test/v1/agents",
            {"data": [{"id": "ag_polly", "name": "polly-review"}]},
        ),
        response(
            "https://omni.test/v1/hosts",
            {
                "hosts": [
                    {
                        "host_id": "host_1",
                        "status": "online",
                        "configured_harnesses": {"claude-sdk": True, "codex": True},
                    }
                ]
            },
        ),
        response("https://omni.test/v1/filesystem", {"data": []}),
    ]
    public_client = mock.MagicMock()
    public_client.__enter__.return_value = public_client
    public_client.__exit__.return_value = None
    public_client.get.return_value = response("https://polly.example.com/health", {"status": "ok"})

    with (
        mock.patch(
            "omnigent.integrations.polly.cli.httpx.Client",
            side_effect=[server_client, public_client],
        ),
        mock.patch("omnigent.integrations.polly.cli._validate_existing_app"),
        mock.patch(
            "omnigent.integrations.polly.github.GitHubAppClient.installation_for",
            new=mock.AsyncMock(return_value=7),
        ),
        mock.patch("omnigent.integrations.polly.cli.secret_store.store_secret") as store,
    ):
        checks = run_doctor()

    assert all(checks.values())
    store.assert_not_called()
