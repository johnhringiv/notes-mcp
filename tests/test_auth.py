"""Tests for the server-side OAuth 2.1 flow: GitHub and access-code logins.

The GitHub exchange and username lookup are faked by overriding
provider.identify / provider.lookup; everything else — DCR, /authorize, our
login page, PKCE verification at /token, bearer enforcement on /mcp — runs
through the real endpoints via an in-process ASGI client.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import re
import secrets
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import git

from notes_mcp.members import GitHubUser

pytestmark = pytest.mark.asyncio

ISSUER = "http://localhost"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"

# GitHub accounts the fake upstream knows, by OAuth code.
USERS = {
    "alice-code": GitHubUser(1, "alice"),  # ADMIN_USERS
    "bob-code": GitHubUser(2, "bob"),  # member added by username
    "bob-renamed-code": GitHubUser(2, "bob-renamed"),  # bob after a rename
    "impostor-code": GitHubUser(99, "bob"),  # someone who took the name "bob"
    "mallory-code": GitHubUser(3, "mallory"),  # nobody
    "alice-impostor-code": GitHubUser(98, "alice"),  # took the admin's name
}


async def fake_identify(code: str) -> GitHubUser:
    return USERS[code]


async def fake_lookup(login: str) -> GitHubUser | None:
    return {"bob": GitHubUser(2, "bob")}.get(login)


def build_server(repo: Path, monkeypatch: pytest.MonkeyPatch, admins: str = "alice") -> ModuleType:
    monkeypatch.setenv("NOTES_REPO_PATH", str(repo))
    monkeypatch.setenv("PUBLIC_URL", ISSUER)
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "gh-client")
    monkeypatch.setenv("ADMIN_USERS", admins)
    monkeypatch.delenv("GITHUB_ALLOWED_LOGIN", raising=False)
    secret_file = repo.parent / "gh_secret"
    secret_file.write_text("gh-secret")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("OAUTH_STATE_DIR", str(repo.parent / "oauth-state"))

    import notes_mcp.server

    server_mod = importlib.reload(notes_mcp.server)
    assert server_mod.auth_provider is not None
    server_mod.auth_provider.identify = fake_identify
    server_mod.auth_provider.lookup = fake_lookup
    return server_mod


@pytest.fixture
def server(repo: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return build_server(repo, monkeypatch)


@pytest.fixture
def app(server: ModuleType) -> Any:
    return server.mcp.streamable_http_app()


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
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def start_login(client: httpx.AsyncClient, client_id: str, challenge: str) -> str:
    """/authorize → our login page; returns the pending state."""
    resp = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "state": "claude-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code in (302, 307), resp.text
    login = urlparse(resp.headers["location"])
    assert login.path == "/auth/login"
    state = parse_qs(login.query)["state"][0]
    page = await client.get("/auth/login", params={"state": state})
    assert page.status_code == 200
    assert "Sign in with GitHub" in page.text and "access code" in page.text
    return state


async def github_login(
    client: httpx.AsyncClient, client_id: str, challenge: str, github_code: str
) -> httpx.Response:
    state = await start_login(client, client_id, challenge)
    resp = await client.get("/auth/github", params={"state": state})
    assert resp.status_code == 302
    github_url = urlparse(resp.headers["location"])
    assert github_url.netloc == "github.com"
    gh_state = parse_qs(github_url.query)["state"][0]
    return await client.get("/auth/callback", params={"code": github_code, "state": gh_state})


async def code_login(
    client: httpx.AsyncClient, client_id: str, challenge: str, access_code: str
) -> httpx.Response:
    state = await start_login(client, client_id, challenge)
    return await client.post("/auth/code", data={"state": state, "code": access_code})


async def exchange(
    client: httpx.AsyncClient, client_id: str, verifier: str, login: httpx.Response
) -> dict[str, Any]:
    assert login.status_code == 302, login.text
    redirect = urlparse(login.headers["location"])
    assert redirect.netloc == "claude.ai"
    query = parse_qs(redirect.query)
    assert query["state"] == ["claude-state"]
    resp = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    token: dict[str, Any] = resp.json()
    return token


async def connect_github(client: httpx.AsyncClient, github_code: str) -> dict[str, Any]:
    info = await register_client(client)
    verifier, challenge = pkce_pair()
    login = await github_login(client, info["client_id"], challenge, github_code)
    return {**await exchange(client, info["client_id"], verifier, login), "client": info}


async def connect_code(client: httpx.AsyncClient, access_code: str) -> dict[str, Any]:
    info = await register_client(client)
    verifier, challenge = pkce_pair()
    login = await code_login(client, info["client_id"], challenge, access_code)
    return {**await exchange(client, info["client_id"], verifier, login), "client": info}


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def mcp_status(client: httpx.AsyncClient, access_token: str) -> int:
    """Status of an MCP initialize with this token. Callers run it inside the
    app lifespan (the session manager needs it; ASGITransport doesn't start
    it, and it can start only once per app)."""
    resp = await client.post(
        "/mcp",
        headers={**MCP_HEADERS, "Authorization": f"Bearer {access_token}"},
        json=INITIALIZE,
    )
    return resp.status_code


async def refresh(client: httpx.AsyncClient, token: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": token["client"]["client_id"],
        },
    )


async def call_tool(
    client: httpx.AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> httpx.Response:
    """initialize → tools/call over streamable HTTP as the token's user."""
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    init = await client.post("/mcp", headers=headers, json=INITIALIZE)
    assert init.status_code == 200, init.text
    headers["Mcp-Session-Id"] = init.headers["mcp-session-id"]
    await client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    resp = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


# ----------------------------------------------------------------------
# discovery and basics


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


async def test_garbage_bearer_token_rejected(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.post("/mcp", headers={"Authorization": "Bearer nonsense"}, json={})
    assert resp.status_code == 401


async def test_login_page_with_bad_state(app_client: httpx.AsyncClient) -> None:
    assert (await app_client.get("/auth/login", params={"state": "bogus"})).status_code == 400
    assert (await app_client.get("/auth/github", params={"state": "bogus"})).status_code == 400
    resp = await app_client.get("/auth/callback", params={"code": "x", "state": "bogus"})
    assert resp.status_code == 400


async def test_login_page_is_locked_down(app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    state = await start_login(app_client, info["client_id"], challenge)
    page = await app_client.get("/auth/login", params={"state": state})
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert "<script" not in page.text


# ----------------------------------------------------------------------
# GitHub logins


async def test_admin_full_flow(app: Any, app_client: httpx.AsyncClient) -> None:
    token = await connect_github(app_client, "alice-code")
    assert token["token_type"].lower() == "bearer"
    async with app.router.lifespan_context(app):
        assert await mcp_status(app_client, token["access_token"]) == 200
    resp = await refresh(app_client, token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] != token["access_token"]


async def test_stranger_is_rejected(app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    resp = await github_login(app_client, info["client_id"], challenge, "mallory-code")
    assert resp.status_code == 403
    assert "mallory" in resp.text


async def test_admin_id_is_pinned_on_first_login(app_client: httpx.AsyncClient) -> None:
    """After alice signs in once, someone who later takes the name is refused."""
    await connect_github(app_client, "alice-code")
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    resp = await github_login(app_client, info["client_id"], challenge, "alice-impostor-code")
    assert resp.status_code == 403


async def test_github_member_matched_by_id_not_name(
    server: ModuleType, app: Any, app_client: httpx.AsyncClient
) -> None:
    server.members.add_member("bob", GitHubUser(2, "bob"))
    token = await connect_github(app_client, "bob-code")
    async with app.router.lifespan_context(app):
        assert await mcp_status(app_client, token["access_token"]) == 200
    # Renaming the account keeps access; a new account with the old name gets none.
    await connect_github(app_client, "bob-renamed-code")
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    resp = await github_login(app_client, info["client_id"], challenge, "impostor-code")
    assert resp.status_code == 403


async def test_wrong_pkce_verifier_rejected(app_client: httpx.AsyncClient) -> None:
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    login = await github_login(app_client, info["client_id"], challenge, "alice-code")
    code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
    resp = await app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": info["client_id"],
            "code_verifier": "not-the-right-verifier-not-the-right-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


# ----------------------------------------------------------------------
# access-code logins


async def test_code_member_full_flow(
    server: ModuleType, app: Any, app_client: httpx.AsyncClient
) -> None:
    server.members.add_member("ann")
    code = server.members.new_code("ann")
    token = await connect_code(app_client, code)
    async with app.router.lifespan_context(app):
        assert await mcp_status(app_client, token["access_token"]) == 200
    assert (await refresh(app_client, token)).status_code == 200
    assert server.members.members()[0].last_login is not None


async def test_wrong_code_rerenders_then_locks(
    server: ModuleType, app_client: httpx.AsyncClient
) -> None:
    server.members.add_member("ann")
    real = server.members.new_code("ann")
    info = await register_client(app_client)
    _, challenge = pkce_pair()
    state = await start_login(app_client, info["client_id"], challenge)
    for attempt in range(1, 6):
        resp = await app_client.post("/auth/code", data={"state": state, "code": "nm_wrong"})
        assert resp.status_code == 403
        assert ("Sign in with GitHub" in resp.text) == (attempt < 5)
    # The pending login is gone; even the right code can't finish it now.
    resp = await app_client.post("/auth/code", data={"state": state, "code": real})
    assert resp.status_code == 400


async def test_replacing_code_ends_sessions(
    server: ModuleType, app: Any, app_client: httpx.AsyncClient
) -> None:
    server.members.add_member("ann")
    token = await connect_code(app_client, server.members.new_code("ann"))
    new_code = server.members.new_code("ann")
    assert (await refresh(app_client, token)).status_code in (400, 401)
    fresh = await connect_code(app_client, new_code)
    async with app.router.lifespan_context(app):
        assert await mcp_status(app_client, token["access_token"]) == 401
        assert await mcp_status(app_client, fresh["access_token"]) == 200


async def test_removed_member_and_admin_are_cut_off(
    server: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = server.mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        server.members.add_member("ann")
        ann = await connect_code(client, server.members.new_code("ann"))
        alice = await connect_github(client, "alice-code")
        server.members.delete_member("ann")
        assert await mcp_status(client, ann["access_token"]) == 401

    # Restart with alice dropped from ADMIN_USERS: her token stops working.
    reborn = build_server(repo, monkeypatch, admins="someone-else")
    app = reborn.mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        assert await mcp_status(client, alice["access_token"]) == 401


# ----------------------------------------------------------------------
# attribution and persistence


async def test_writes_are_credited_to_the_calling_user(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each principal's commit carries their identity as author, read per request."""
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed")
    server = build_server(repo, monkeypatch)
    server.members.add_member("ann")
    server.members.set_grant("ann", "ann-notes", "rw")
    code = server.members.new_code("ann")
    app = server.mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        alice = await connect_github(client, "alice-code")
        ann = await connect_code(client, code)
        async with app.router.lifespan_context(app):
            await call_tool(
                client, alice["access_token"], "create_note", {"note_id": "a-note", "title": "A"}
            )
            await call_tool(
                client, ann["access_token"], "create_note", {"note_id": "ann-notes/b", "title": "B"}
            )
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-2", "--format=%s|%an|%ae"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert log == [
        "Create note: ann-notes/b|ann|ann@members.notes-mcp.invalid",
        "Create note: a-note|alice|1+alice@users.noreply.github.com",
    ]


async def test_tokens_and_members_survive_restart(
    server: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signing secret, clients and members persist across a container restart."""
    server.members.add_member("ann")
    server.members.set_grant("ann", "kitchen", "rw")
    code = server.members.new_code("ann")
    app = server.mcp.streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        token = await connect_code(client, code)

    reborn = build_server(repo, monkeypatch)
    assert reborn.members.login_code(code) == "member:ann"
    assert reborn.members.members()[0].grants[0].path == "kitchen"
    access = await reborn.auth_provider.load_access_token(token["access_token"])
    assert access is not None and access.subject == "member:ann"
    state_file = repo.parent / "oauth-state" / "members.json"
    assert code not in state_file.read_text()  # only the hash is stored
    assert re.search(r'"code_hash": "[0-9a-f]{64}"', state_file.read_text())


async def test_expired_and_wrong_type_tokens_rejected(server: ModuleType) -> None:
    server.members.login_github(GitHubUser(1, "alice"))
    provider = server.auth_provider
    expired = provider._mint("access", "c", "admin:1", -10)
    assert await provider.load_access_token(expired) is None
    refresh_token = provider._mint("refresh", "c", "admin:1", 3600)
    assert await provider.load_access_token(refresh_token) is None  # type confusion blocked
    session = provider._mint("admin_session", "admin", "admin:1", 3600)
    assert await provider.load_access_token(session) is None


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


async def test_refuses_to_start_with_auth_and_no_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Members are promised recoverable, attributed writes; that needs git."""
    server = build_server(repo, monkeypatch)
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: pytest.fail("server started"))
    with pytest.raises(SystemExit):
        server.main()
