from __future__ import annotations

import html
import json
import os
import secrets as stdlib_secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import parse_qs, quote, urlparse

import click
import httpx

from omnigent.host.local_server import _local_data_dir
from omnigent.integration_daemon import IntegrationDaemon
from omnigent.integrations.polly.store import PollyStore
from omnigent.onboarding import secrets as secret_store

GITHUB_PRIVATE_KEY_SECRET = "polly-github-private-key"
WEBHOOK_SECRET_SECRET = "polly-github-webhook-secret"
OMNIGENT_ACCESS_TOKEN_SECRET = "polly-omnigent-access-token"
OMNIGENT_REFRESH_TOKEN_SECRET = "polly-omnigent-refresh-token"
OMNIGENT_DEVICE_CLIENT_SECRET = "polly-omnigent-device-client-secret"
_DEVICE_CLIENT_SECRET_HEADER = "X-Omnigent-Client-Secret"
_GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class PollyConfig:
    server_url: str
    public_url: str
    listen_host: str
    listen_port: int
    workspace: str
    agent_id: str
    host_id: str
    github_app_id: str
    github_app_slug: str
    repositories: tuple[str, ...]


def integration_dir() -> Path:
    return _local_data_dir() / "integrations" / "polly"


def config_path() -> Path:
    return integration_dir() / "config.json"


def database_path() -> Path:
    return integration_dir() / "polly.sqlite3"


def save_config(config: PollyConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True) + "\n")


def load_config() -> PollyConfig:
    try:
        raw = json.loads(config_path().read_text())
        raw["repositories"] = tuple(raw["repositories"])
        return PollyConfig(**raw)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise click.ClickException(
            "Polly is not configured. Run `omni integration polly setup`."
        ) from exc


def _validate_origin(value: str, label: str, *, allow_loopback_http: bool) -> str:
    parsed = urlparse(value)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} URL must be a valid origin") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} URL must be an origin without credentials, path, or query")
    if parsed.scheme != "https" and not (
        allow_loopback_http and parsed.scheme == "http" and loopback
    ):
        raise ValueError(f"{label} URL must use HTTPS")
    return value[:-1] if parsed.path == "/" else value


def validate_public_url(value: str, *, allow_loopback_http: bool = False) -> str:
    return _validate_origin(value, "public", allow_loopback_http=allow_loopback_http)


def validate_server_url(value: str) -> str:
    return _validate_origin(value, "server", allow_loopback_http=True)


def validate_port(value: int) -> int:
    if not 1 <= value <= 65535:
        raise ValueError("listener port must be between 1 and 65535")
    return value


def validate_workspace(value: str) -> str:
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    parts = path.parts[1:]
    direct_home = len(parts) == 2 and parts[0] in {"home", "Users"}
    if (
        not path.is_absolute()
        or "." in raw_parts
        or ".." in raw_parts
        or not parts
        or parts == ("root",)
        or direct_home
    ):
        raise ValueError(
            "workspace must be an absolute dedicated directory, not a filesystem root or home"
        )
    return str(path)


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"} if access_token else {}


def acquire_device_tokens(
    server_url: str,
    *,
    device_client_secret: str = "",
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    own_client = client is None
    client = client or httpx.Client(base_url=server_url, timeout=15)
    try:
        headers = (
            {_DEVICE_CLIENT_SECRET_HEADER: device_client_secret} if device_client_secret else {}
        )
        response = client.post(
            "/oauth/device/authorize", json={"client_id": "polly"}, headers=headers
        )
        if response.status_code == 404:
            raise click.ClickException(
                "The server does not offer scoped device grants. Accounts or OIDC with "
                "OMNIGENT_DEVICE_GRANT_ENABLED=1 is required; header/proxy auth is unsupported."
            )
        response.raise_for_status()
        grant = response.json()
        click.echo(f"Authorize Polly in your browser: {grant['verification_uri_complete']}")
        webbrowser.open(str(grant["verification_uri_complete"]))
        deadline = time.monotonic() + int(grant.get("expires_in", 600))
        interval = max(1, int(grant.get("interval", 5)))
        while time.monotonic() < deadline:
            token = client.post(
                "/oauth/token",
                headers=headers,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": grant["device_code"],
                },
            )
            if token.status_code == 200:
                payload = token.json()
                access = payload.get("access_token") if isinstance(payload, dict) else None
                refresh = payload.get("refresh_token") if isinstance(payload, dict) else None
                if not isinstance(access, str) or not isinstance(refresh, str):
                    raise click.ClickException("Device authorization returned invalid tokens.")
                access, refresh = access.strip(), refresh.strip()
                if not access or not refresh:
                    raise click.ClickException("Device authorization returned invalid tokens.")
                return access, refresh
            error = (
                token.json().get("error")
                if token.headers.get("content-type", "").startswith("application/json")
                else None
            )
            if error == "slow_down":
                interval += 5
            elif error != "authorization_pending":
                token.raise_for_status()
            time.sleep(interval)
        raise click.ClickException("Device authorization expired before approval.")
    finally:
        if own_client:
            client.close()


def exchange_manifest_code(
    callback_url: str, *, expected_state: str, client: httpx.Client | None = None
) -> dict[str, object]:
    query = parse_qs(urlparse(callback_url).query)
    if query.get("state") != [expected_state]:
        raise ValueError("GitHub App manifest callback state did not match")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("GitHub App manifest callback carried no code")
    own_client = client is None
    client = client or httpx.Client(base_url=_GITHUB_API, timeout=30)
    try:
        response = client.post(f"{_GITHUB_API}/app-manifests/{quote(code, safe='')}/conversions")
        response.raise_for_status()
        payload = response.json()
    finally:
        if own_client:
            client.close()
    required = ("id", "slug", "pem", "webhook_secret")
    if not isinstance(payload, dict) or any(not payload.get(key) for key in required):
        raise ValueError("GitHub manifest conversion response was incomplete")
    return cast(dict[str, object], payload)


def _manifest_registration_url(organization: str | None) -> str:
    if organization is None:
        return "https://github.com/settings/apps/new"
    organization = organization.strip()
    if not organization or "/" in organization:
        raise click.ClickException("GitHub organization must be one account name.")
    return f"https://github.com/organizations/{quote(organization, safe='')}/settings/apps/new"


def _manifest_flow(public_url: str, organization: str | None = None) -> dict[str, object]:
    state = stdlib_secrets.token_urlsafe(24)
    callback: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                callback.append(f"http://127.0.0.1{self.path}")
                body = b"GitHub App created. Return to the terminal."
            else:
                manifest = {
                    "name": "Omnigent Polly Review",
                    "url": public_url,
                    "redirect_url": f"http://127.0.0.1:{server.server_port}/callback?state={state}",
                    "hook_attributes": {"url": f"{public_url}/webhooks/github", "active": True},
                    "public": False,
                    "default_permissions": {"pull_requests": "write"},
                    "default_events": ["pull_request"],
                }
                body = (
                    '<form id="f" method="post" action="'
                    f'{_manifest_registration_url(organization)}">'
                    '<input type="hidden" name="manifest" value="'
                    f'{html.escape(json.dumps(manifest), quote=True)}">'
                    '<button type="submit">Create GitHub App</button></form>'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: ARG002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        click.echo(f"Create the least-privilege GitHub App in your browser: {url}")
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while not callback and time.monotonic() < deadline:
            time.sleep(0.1)
        if not callback:
            raise click.ClickException("Timed out waiting for the GitHub App manifest callback.")
        return exchange_manifest_code(callback[0], expected_state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _validate_server_selection(
    server_url: str, access_token: str, workspace: str, host_selector: str | None
) -> tuple[str, str]:
    headers = _auth_headers(access_token)
    with httpx.Client(base_url=server_url, headers=headers, timeout=30) as client:
        agents = client.get("/v1/agents", params={"limit": 1000})
        agents.raise_for_status()
        matches = [
            item for item in agents.json().get("data", []) if item.get("name") == "polly-review"
        ]
        if len(matches) != 1:
            raise click.ClickException(
                "The bundled `polly-review` agent is not available exactly once."
            )
        hosts = client.get("/v1/hosts")
        hosts.raise_for_status()
        ready = [
            host
            for host in hosts.json().get("hosts", [])
            if host.get("status") == "online"
            and host.get("configured_harnesses", {}).get("claude-sdk") is True
            and host.get("configured_harnesses", {}).get("codex") is True
        ]
        if host_selector is not None:
            selected = [
                host
                for host in ready
                if host.get("host_id") == host_selector or host.get("name") == host_selector
            ]
            if len(selected) != 1:
                available = (
                    ", ".join(
                        f"{host.get('name') or '<unnamed>'} ({host.get('host_id')})"
                        for host in ready
                    )
                    or "none"
                )
                raise click.ClickException(
                    f"--host {host_selector!r} did not identify one ready host. "
                    f"Ready hosts: {available}."
                )
        elif len(ready) == 1:
            selected = ready
        elif not ready:
            raise click.ClickException("No online host reports claude-sdk=True and codex=True.")
        else:
            available = ", ".join(
                f"{host.get('name') or '<unnamed>'} ({host.get('host_id')})" for host in ready
            )
            raise click.ClickException(
                f"Multiple ready hosts found; select one with --host ID_OR_NAME: {available}."
            )
        host_id = str(selected[0]["host_id"])
        path = quote(workspace.lstrip("/"), safe="/")
        listing = client.get(f"/v1/hosts/{quote(host_id, safe='')}/filesystem/{path}")
        if listing.status_code == 404:
            raise click.ClickException(
                f"Workspace does not exist on host {host_id}. Create {workspace} there, "
                "leave it empty, and rerun setup."
            )
        listing.raise_for_status()
        if listing.json().get("data") != []:
            raise click.ClickException(f"Workspace {workspace} must already exist and be empty.")
        return str(matches[0]["id"]), host_id


def _github_app_jwt(app_id: str, private_key: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, algorithm="RS256"
    )


def _validate_existing_app(app_id: str, app_slug: str, private_key: str) -> None:
    token = _github_app_jwt(app_id, private_key)
    with httpx.Client(
        base_url=_GITHUB_API, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        response = client.get("/app")
        response.raise_for_status()
        app = response.json()
    if app.get("slug") != app_slug:
        raise click.ClickException("GitHub App slug does not match the supplied App ID/key.")
    permissions = app.get("permissions", {})
    if permissions.get("pull_requests") != "write" or set(permissions) - {
        "pull_requests",
        "metadata",
    }:
        raise click.ClickException("GitHub App permissions must be only Pull requests: write.")
    if set(app.get("events", [])) != {"pull_request"}:
        raise click.ClickException("GitHub App events must be only pull_request.")


def _validate_repository_installations(
    app_id: str, private_key: str, repositories: tuple[str, ...]
) -> None:
    token = _github_app_jwt(app_id, private_key)
    with httpx.Client(
        base_url=_GITHUB_API, headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as client:
        for repository in repositories:
            owner, repo = repository.split("/", 1)
            response = client.get(
                f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation"
            )
            if response.status_code == 404:
                raise click.ClickException(
                    f"GitHub App is not installed on selected repository {repository}."
                )
            response.raise_for_status()


def _run_setup(
    *,
    server: str,
    public_url: str,
    listen_host: str,
    listen_port: int,
    workspace: str,
    host: str | None,
    app_id: str | None,
    app_slug: str | None,
    private_key_file: Path | None,
    repositories: tuple[str, ...],
    allow_loopback_http: bool,
    device_client_secret_file: Path | None = None,
    github_organization: str | None = None,
) -> PollyConfig:
    server = validate_server_url(server)
    public_url = validate_public_url(public_url, allow_loopback_http=allow_loopback_http)
    listen_port = validate_port(listen_port)
    workspace = validate_workspace(workspace)
    device_client_secret = os.environ.get("OMNIGENT_DEVICE_CLIENT_SECRET", "").strip()
    if device_client_secret_file is not None:
        try:
            device_client_secret = device_client_secret_file.read_text().strip()
        except OSError as exc:
            raise click.ClickException(f"Could not read device client secret file: {exc}") from exc
        if not device_client_secret:
            raise click.ClickException("Device client secret file is empty.")
    importing = any(value is not None for value in (app_id, app_slug, private_key_file))
    private_key = ""
    webhook_secret = ""
    if importing:
        if app_id is None or app_slug is None or private_key_file is None:
            raise click.ClickException(
                "Existing-App import requires --app-id, --app-slug, and --private-key-file."
            )
        try:
            private_key = private_key_file.read_text()
        except OSError as exc:
            raise click.ClickException(f"Could not read private key file: {exc}") from exc
        if not private_key.strip():
            raise click.ClickException("GitHub private key must not be empty.")
    access_token, refresh_token = acquire_device_tokens(
        server, device_client_secret=device_client_secret
    )
    agent_id, host_id = _validate_server_selection(server, access_token, workspace, host)

    if importing:
        webhook_secret = click.prompt("GitHub webhook secret", hide_input=True)
        if not webhook_secret.strip():
            raise click.ClickException("GitHub webhook secret must not be empty.")
        _validate_existing_app(str(app_id), str(app_slug), private_key)
    else:
        app = _manifest_flow(public_url, github_organization)
        app_id, app_slug = str(app["id"]), str(app["slug"])
        private_key, webhook_secret = str(app["pem"]), str(app["webhook_secret"])
        if not private_key.strip() or not webhook_secret.strip():
            raise click.ClickException("GitHub App returned an empty required credential.")
        install_url = f"https://github.com/apps/{quote(app_slug, safe='')}/installations/new"
        click.echo(f"Install the GitHub App on the repositories to review: {install_url}")
        webbrowser.open(install_url)

    selected = repositories or tuple(
        part.strip()
        for part in click.prompt("Installed repositories (owner/name, comma-separated)").split(",")
        if part.strip()
    )
    if not selected or any(repo.count("/") != 1 for repo in selected):
        raise click.ClickException("Select at least one installed repository as owner/name.")
    _validate_repository_installations(str(app_id), private_key, selected)

    config = PollyConfig(
        server_url=server.rstrip("/"),
        public_url=public_url,
        listen_host=listen_host,
        listen_port=listen_port,
        workspace=workspace,
        agent_id=agent_id,
        host_id=host_id,
        github_app_id=str(app_id),
        github_app_slug=str(app_slug),
        repositories=selected,
    )
    secret_store.store_secret(GITHUB_PRIVATE_KEY_SECRET, private_key)
    secret_store.store_secret(WEBHOOK_SECRET_SECRET, webhook_secret)
    secret_store.store_secret(OMNIGENT_ACCESS_TOKEN_SECRET, access_token)
    secret_store.store_secret(OMNIGENT_REFRESH_TOKEN_SECRET, refresh_token)
    secret_store.store_secret(OMNIGENT_DEVICE_CLIENT_SECRET, device_client_secret)
    save_config(config)
    click.echo(f"Polly configured. GitHub webhook: {public_url}/webhooks/github")
    return config


def _daemon() -> IntegrationDaemon:
    return IntegrationDaemon("polly", _local_data_dir())


def _runtime_argv() -> list[str]:
    return [sys.executable, "-m", "omnigent.integrations.polly.runtime"]


def _start_background() -> None:
    load_config()
    daemon = _daemon()
    current = daemon.running_record()
    if current:
        click.echo(f"Polly already running (pid {current.pid}).")
        return
    record = daemon.start(_runtime_argv(), os.environ.copy())
    if not daemon.confirm_alive(record, grace_seconds=2):
        tail = daemon.read_log_tail()
        raise click.ClickException("Polly exited immediately." + (f"\n{tail}" if tail else ""))
    click.echo(f"Started Polly in the background (pid {record.pid}).")


def run_doctor() -> dict[str, bool]:
    checks = dict.fromkeys(
        (
            "config",
            "secrets",
            "access token",
            "agent",
            "host",
            "harness readiness",
            "workspace",
            "GitHub App",
            "installation repositories",
            "public health",
        ),
        False,
    )
    try:
        config = load_config()
        checks["config"] = True
        private_key = secret_store.load_secret(GITHUB_PRIVATE_KEY_SECRET)
        webhook = secret_store.load_secret(WEBHOOK_SECRET_SECRET)
        refresh = secret_store.load_secret(OMNIGENT_REFRESH_TOKEN_SECRET)
        access = secret_store.load_secret(OMNIGENT_ACCESS_TOKEN_SECRET)
        checks["secrets"] = all(
            isinstance(value, str) and bool(value.strip())
            for value in (private_key, webhook, refresh, access)
        )
        if not checks["secrets"]:
            return checks
        assert isinstance(access, str)
        headers = _auth_headers(access)
        with httpx.Client(base_url=config.server_url, headers=headers, timeout=15) as client:
            agent_response = client.get("/v1/agents", params={"limit": 1000})
            agent_response.raise_for_status()
            checks["access token"] = True
            agents = agent_response.json().get("data", [])
            checks["agent"] = any(
                a.get("id") == config.agent_id and a.get("name") == "polly-review" for a in agents
            )
            hosts = client.get("/v1/hosts").json().get("hosts", [])
            host = next((h for h in hosts if h.get("host_id") == config.host_id), None)
            checks["host"] = bool(host and host.get("status") == "online")
            readiness = host.get("configured_harnesses", {}) if host else {}
            checks["harness readiness"] = (
                readiness.get("claude-sdk") is True and readiness.get("codex") is True
            )
            path = quote(config.workspace.lstrip("/"), safe="/")
            listing = client.get(f"/v1/hosts/{quote(config.host_id, safe='')}/filesystem/{path}")
            checks["workspace"] = listing.status_code == 200 and listing.json().get("data") == []
        if private_key:
            _validate_existing_app(config.github_app_id, config.github_app_slug, private_key)
            checks["GitHub App"] = True
            import asyncio

            from omnigent.integrations.polly.github import GitHubAppClient

            async def repositories_installed() -> bool:
                async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=15) as github_http:
                    github = GitHubAppClient(
                        config.github_app_id,
                        private_key,
                        github_http,
                        app_slug=config.github_app_slug,
                    )
                    for repository in config.repositories:
                        owner, repo = repository.split("/", 1)
                        await github.installation_for(owner, repo)
                return True

            checks["installation repositories"] = asyncio.run(repositories_installed())
        with httpx.Client(timeout=15) as client:
            response = client.get(f"{config.public_url}/health")
            checks["public health"] = (
                response.status_code == 200 and response.json().get("status") == "ok"
            )
    except (click.ClickException, httpx.HTTPError, KeyError, TypeError, ValueError):
        pass
    return checks


def register_polly_command(integration: click.Group) -> None:
    @integration.group("polly", invoke_without_command=True)
    @click.option("--background", is_flag=True)
    @click.pass_context
    def polly(ctx: click.Context, background: bool) -> None:
        """Run and manage native Polly pull-request reviews."""
        if ctx.invoked_subcommand is not None:
            return
        if background:
            _start_background()
            return
        load_config()
        current = _daemon().running_record()
        if current:
            raise click.ClickException(f"Polly is already running (pid {current.pid}).")
        result = subprocess.run(_runtime_argv(), env=os.environ.copy(), check=False)
        raise SystemExit(result.returncode)

    @polly.command("status")
    def status() -> None:
        record = _daemon().running_record()
        click.echo(f"Polly: running (pid {record.pid})." if record else "Polly: not running.")

    @polly.command("stop")
    def stop() -> None:
        record = _daemon().stop()
        click.echo(f"Stopped Polly (pid {record.pid})." if record else "Polly: not running.")

    @polly.command("logs")
    @click.option("-f", "--follow", is_flag=True)
    def logs(follow: bool) -> None:
        record = _daemon().read_record()
        if not record:
            click.echo("No Polly daemon has been started yet.")
            return
        if follow:
            subprocess.run(["tail", "-f", record.log_path], check=False)
        else:
            click.echo(record.log_path)

    @polly.command("setup")
    @click.option("--server", required=True)
    @click.option("--public-url", required=True)
    @click.option("--listen-host", default="127.0.0.1", show_default=True)
    @click.option("--listen-port", default=8788, type=int, show_default=True)
    @click.option("--workspace", required=True)
    @click.option("--host")
    @click.option("--app-id")
    @click.option("--app-slug")
    @click.option("--private-key-file", type=click.Path(path_type=Path))
    @click.option("--device-client-secret-file", type=click.Path(path_type=Path))
    @click.option("--github-organization")
    @click.option("--repository", "repositories", multiple=True)
    @click.option("--allow-loopback-http", is_flag=True, hidden=True)
    def setup(
        server: str,
        public_url: str,
        listen_host: str,
        listen_port: int,
        workspace: str,
        host: str | None,
        app_id: str | None,
        app_slug: str | None,
        private_key_file: Path | None,
        device_client_secret_file: Path | None,
        github_organization: str | None,
        repositories: tuple[str, ...],
        allow_loopback_http: bool,
    ) -> None:
        if _daemon().running_record():
            raise click.ClickException("Stop the running Polly daemon before setup.")
        try:
            _run_setup(
                server=server,
                public_url=public_url,
                listen_host=listen_host,
                listen_port=listen_port,
                workspace=workspace,
                host=host,
                app_id=app_id,
                app_slug=app_slug,
                private_key_file=private_key_file,
                device_client_secret_file=device_client_secret_file,
                github_organization=github_organization,
                repositories=repositories,
                allow_loopback_http=allow_loopback_http,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    @polly.command("doctor")
    def doctor() -> None:
        checks = run_doctor()
        for name, passed in checks.items():
            click.echo(f"{name}: {'OK' if passed else 'FAIL'}")
        if not all(checks.values()):
            raise click.ClickException("Polly doctor found problems.")

    @polly.command("retry")
    @click.argument("job_id", type=int)
    def retry(job_id: int) -> None:
        store = PollyStore(database_path())
        try:
            store.get_job(job_id)
        except KeyError as exc:
            raise click.ClickException(f"Polly job {job_id} does not exist.") from exc
        if not store.reset_failed(job_id):
            raise click.ClickException(f"Polly job {job_id} is not failed.")
        click.echo(f"Polly job {job_id} queued for retry.")

    @polly.command("jobs")
    def jobs() -> None:
        for job in PollyStore(database_path()).list_jobs():
            error = job.error or "-"
            for name in (
                GITHUB_PRIVATE_KEY_SECRET,
                WEBHOOK_SECRET_SECRET,
                OMNIGENT_ACCESS_TOKEN_SECRET,
                OMNIGENT_REFRESH_TOKEN_SECRET,
                OMNIGENT_DEVICE_CLIENT_SECRET,
            ):
                secret = secret_store.load_secret(name)
                if secret and secret.strip():
                    error = error.replace(secret, "[redacted]")
                    error = error.replace(" ".join(secret.split()), "[redacted]")
            error = " ".join(error.split())[:200]
            click.echo(
                f"{job.id}\t{job.owner}/{job.repo}\t#{job.pr_number}\t{job.head_sha}\t"
                f"{job.state}\t{error}"
            )
