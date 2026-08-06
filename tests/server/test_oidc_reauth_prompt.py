"""Forced re-authentication survives the handoff to an OIDC IdP.

The device-grant consent page refuses to render for a session that predates
the grant being approved, and bounces through the login page with
``?reauth=1``. That is the anti-phishing gate: approving a device grant must
cost a deliberate credential entry, so a victim handed a one-click link cannot
bind an attacker's grant by reflex.

In accounts mode the SPA login form enforces it — it holds back its
auto-redirect and demands a password. OIDC has no form to hold back; the IdP
owns the credential. The only way to ask is the OIDC ``prompt`` parameter, so
``/auth/login`` must forward ``reauth=1`` as ``prompt=login``.

Without that forwarding the flow still *works* — the IdP satisfies the bounce
from its own session, returns a token, and the callback mints a session whose
fresh ``iat`` clears the consent gate. Nothing errors, no test fails, and the
gate silently approves a user who proved nothing. These tests pin the
parameter so that regression cannot pass quietly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.admin_list import AdminList
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import OIDCConfig
from omnigent.server.routes.auth import create_auth_router
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_TEST_SECRET = bytes.fromhex("aa" * 32)


def _oidc_config() -> OIDCConfig:
    """An OIDC config over plain HTTP so TestClient handles cookies."""
    return OIDCConfig(
        issuer="https://accounts.google.com",
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        cookie_secret=_TEST_SECRET,
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


@pytest.fixture
def oidc_client(tmp_path: Path, db_uri: str) -> Iterator[TestClient]:
    """An OIDC auth router mounted on a TestClient, redirects not followed."""
    perm_store = SqlAlchemyPermissionStore(db_uri)
    admins = tmp_path / "admins"
    admins.write_text("")
    provider = UnifiedAuthProvider(source="oidc", oidc_config=_oidc_config())

    app = FastAPI()
    app.include_router(
        create_auth_router(provider, perm_store, AdminList(admins)),
        prefix="/auth",
    )
    with TestClient(app) as client:
        yield client


def _authorize_params(client: TestClient, query: str) -> dict[str, list[str]]:
    """Follow ``/auth/login`` one hop and return the IdP authorize params."""
    res = client.get(f"/auth/login{query}", follow_redirects=False)
    assert res.status_code == 302, res.status_code
    return parse_qs(urlparse(res.headers["location"]).query)


def test_reauth_forwards_prompt_login_to_the_idp(oidc_client: TestClient) -> None:
    """``?reauth=1`` must reach the IdP as ``prompt=login``.

    This is the whole gate under OIDC. Drop the parameter and the IdP
    re-authenticates the user silently from its own session.
    """
    params = _authorize_params(oidc_client, "?reauth=1&return_to=/oauth/device")

    assert params.get("prompt") == ["login"]
    # The rest of the request must be unchanged — PKCE and state still apply.
    assert params["code_challenge_method"] == ["S256"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge"] and params["state"]


def test_an_ordinary_login_does_not_re_prompt(oidc_client: TestClient) -> None:
    """No ``reauth`` ⇒ no ``prompt``.

    Sending ``prompt=login`` unconditionally would force a password entry on
    every single sign-in, which is how a security control ends up switched
    off by whoever finds it annoying.
    """
    assert "prompt" not in _authorize_params(oidc_client, "")
    assert "prompt" not in _authorize_params(oidc_client, "?return_to=/sessions")


@pytest.mark.parametrize("raw", ["0", "true", "yes", "", "1 ", "TRUE"])
def test_only_an_exact_1_forces_re_authentication(oidc_client: TestClient, raw: str) -> None:
    """Anything other than exactly ``1`` is not a re-auth request.

    Matched strictly because the consent page is the only caller and it sends
    exactly ``1``; accepting loose truthy spellings would let an unrelated
    query param turn on a re-prompt nobody asked for.
    """
    assert "prompt" not in _authorize_params(oidc_client, f"?reauth={raw}")


def test_re_authentication_still_round_trips_the_return_to(oidc_client: TestClient) -> None:
    """The consent URL must survive the re-auth bounce.

    Losing it would land the user on the dashboard after re-entering their
    password, with the pending grant abandoned and no way back to it.
    """
    from urllib.parse import unquote

    import jwt

    oidc_client.get(
        "/auth/login?reauth=1&return_to=/oauth/device%3Fuser_code%3DK7M2-QP9X",
        follow_redirects=False,
    )
    cookie = oidc_client.cookies.get("ap_auth_state")
    assert cookie is not None
    claims = jwt.decode(cookie, _TEST_SECRET, algorithms=["HS256"])

    assert unquote(claims["return_to"]) == "/oauth/device?user_code=K7M2-QP9X"
