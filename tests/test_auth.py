"""Tests for the server-side OAuth 2.1 flow (GitHub upstream identity).

The GitHub exchange is faked by overriding _github_login_for_code; everything
else — DCR, /authorize, PKCE verification at /token, bearer enforcement on
/mcp — runs through the real SDK endpoints via an in-process ASGI client.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from notes_mcp.auth import GitHubOAuthProvider

pytestmark = pytest.mark.asyncio

ISSUER = "http://localhost"


@pytest.fixture
def provider(tmp_path: Path) -> GitHubOAuthProvider:
    p = GitHubOAuthProvider(
        issuer_url=ISSUER,
        github_client_id="gh-client",
        github_client_secret="gh-secret",
        allowed_login="alice",
        state_dir=tmp_path / "state",
    )

    async def fake_github(code: str) -> str:
        return {"good-code": "alice", "stranger-code": "mallory"}[code]

    p.github_exchange = fake_github
    return p


@pytest.fixture
def app(provider: GitHubOAuthProvider, repo: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real server ASGI app (auth enabled)."""
    monkeypatch.setenv("NOTES_REPO_PATH", str(repo))
    monkeypatch.setenv("PUBLIC_URL", ISSUER)
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "gh-client")
    monkeypatch.setenv("GITHUB_ALLOWED_LOGIN", "alice")
    secret_file = repo.parent / "gh_secret"
    secret_file.write_text("gh-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("OAUTH_STATE_DIR", str(repo.parent / "oauth-state"))

    import notes_mcp.server

    server_mod = importlib.reload(notes_mcp.server)
    assert server_mod.auth_provider is not None
    server_mod.auth_provider.github_exchange = provider.github_exchange
    return server_mod.mcp.streamable_http_app()


@pytest.fixture
async def app_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        yield client


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


async def register_client(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def run_authorize(
    client: httpx.AsyncClient, client_id: str, challenge: str, github_code: str = "good-code"
) -> httpx.Response:
    resp = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "state": "claude-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code in (302, 307), resp.text
    github_url = urlparse(resp.headers["location"])
    assert github_url.netloc == "github.com"
    gh_state = parse_qs(github_url.query)["state"][0]
    return await client.get("/auth/callback", params={"code": github_code, "state": gh_state})


# ----------------------------------------------------------------------


async def test_mcp_requires_token_with_www_authenticate(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post("/mcp", json={})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


async def test_discovery_endpoints(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    assert resp.json()["authorization_servers"] == [f"{ISSUER}/"]
    resp = await app_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"] == f"{ISSUER}/register"


async def test_full_flow_issues_working_token(app: Any, app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    verifier, challenge = pkce_pair()

    callback = await run_authorize(app_client, info["client_id"], challenge)
    assert callback.status_code == 302
    redirect = urlparse(callback.headers["location"])
    assert redirect.netloc == "claude.ai"
    query = parse_qs(redirect.query)
    assert query["state"] == ["claude-state"]
    code = query["code"][0]

    resp = await app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": info["client_id"],
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()
    assert token["token_type"].lower() == "bearer"

    # The access token opens the MCP endpoint. The streamable HTTP session
    # manager needs the app lifespan; ASGITransport doesn't run it, so enter
    # it here (same task in and out — anyio requires that).
    async with app.router.lifespan_context(app):
        resp = await app_client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert resp.status_code == 200, resp.text

    # And the refresh grant rotates tokens.
    resp = await app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": info["client_id"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] != token["access_token"]


async def test_wrong_github_user_is_rejected(app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    callback = await run_authorize(
        app_client, info["client_id"], challenge, github_code="stranger-code"
    )
    assert callback.status_code == 403
    assert "mallory" in callback.json()["details"]


async def test_wrong_pkce_verifier_rejected(app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    callback = await run_authorize(app_client, info["client_id"], challenge)
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    resp = await app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": info["client_id"],
            "code_verifier": "not-the-right-verifier-not-the-right-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_callback_with_bad_state(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/auth/callback", params={"code": "x", "state": "bogus"})
    assert resp.status_code == 400


async def test_garbage_bearer_token_rejected(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post("/mcp", headers={"Authorization": "Bearer nonsense"}, json={})
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# provider unit behavior


async def test_tokens_survive_provider_restart(tmp_path: Path) -> None:
    """Signing secret + clients persist, so a container restart keeps tokens valid."""
    state = tmp_path / "state"

    def make() -> GitHubOAuthProvider:
        return GitHubOAuthProvider(
            issuer_url=ISSUER,
            github_client_id="c",
            github_client_secret="s",
            allowed_login="alice",
            state_dir=state,
        )

    first = make()
    token = first._mint("access", "client-1", "alice", 3600)

    reborn = make()
    access = await reborn.load_access_token(token)
    assert access is not None
    assert access.subject == "alice"


async def test_expired_and_wrong_type_tokens_rejected(provider: GitHubOAuthProvider) -> None:
    expired = provider._mint("access", "c", "alice", -10)
    assert await provider.load_access_token(expired) is None
    refresh = provider._mint("refresh", "c", "alice", 3600)
    assert await provider.load_access_token(refresh) is None  # type confusion blocked


async def test_registration_cap(app_client: httpx.AsyncClient) -> None:
    from notes_mcp import auth as auth_mod

    original = auth_mod.MAX_CLIENTS
    auth_mod.MAX_CLIENTS = 2
    try:
        assert (await register_client(app_client))["client_id"]
        assert (await register_client(app_client))["client_id"]
        resp = await app_client.post(
            "/register",
            json={
                "client_name": "bot",
                "redirect_uris": ["https://evil.example/cb"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert resp.status_code == 400
        assert "limit" in resp.text
    finally:
        auth_mod.MAX_CLIENTS = original
