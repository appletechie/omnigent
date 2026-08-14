from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_SECRET = bytes.fromhex("aa" * 32)


@pytest.fixture
def oidc_client(tmp_path: Path, db_uri: str) -> Iterator[TestClient]:
    config = OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_SECRET,
        scopes="openid email profile",
        session_ttl_hours=8,
        logout_redirect_uri=None,
        allowed_domains=None,
        provider_type="oidc",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        userinfo_endpoint=None,
        allow_invites=False,
    )
    admins = tmp_path / "admins"
    admins.write_text("")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=config)
    app = FastAPI()
    app.include_router(
        create_auth_router(provider, SqlAlchemyPermissionStore(db_uri), AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client


def test_reauth_request_is_forwarded_and_attested(oidc_client: TestClient) -> None:
    response = oidc_client.get(
        "/auth/login?reauth=1&return_to=%2Foauth%2Fdevice%3Fuser_code%3DABCD-2345",
        follow_redirects=False,
    )
    params = parse_qs(urlsplit(response.headers["location"]).query)
    state = jwt.decode(oidc_client.cookies["ap_auth_state"], _SECRET, algorithms=["HS256"])

    assert params["prompt"] == ["login"]
    assert params["max_age"] == ["0"]
    assert isinstance(state["reauth_at"], int)
    assert state["return_to"] == "/oauth/device?user_code=ABCD-2345"


def test_ordinary_login_does_not_force_reauthentication(oidc_client: TestClient) -> None:
    response = oidc_client.get("/auth/login", follow_redirects=False)
    params = parse_qs(urlsplit(response.headers["location"]).query)
    state = jwt.decode(oidc_client.cookies["ap_auth_state"], _SECRET, algorithms=["HS256"])

    assert "prompt" not in params and "max_age" not in params
    assert "reauth_at" not in state
