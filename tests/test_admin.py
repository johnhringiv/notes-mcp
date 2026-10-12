"""Tests for the admin page: who gets in, CSRF, and the member operations."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import make_note
from test_auth import build_server, connect_code, connect_github, mcp_status

from notes_mcp.admin import SESSION_COOKIE

pytestmark = pytest.mark.asyncio


@pytest.fixture
def server(repo: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return build_server(repo, monkeypatch)


@pytest.fixture
def app(server: ModuleType) -> Any:
    return server.mcp.streamable_http_app()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def admin_login(client: httpx.AsyncClient, github_code: str = "alice-code") -> httpx.Response:
    resp = await client.get("/admin/login")
    assert resp.status_code == 302
    gh = urlparse(resp.headers["location"])
    assert gh.netloc == "github.com"
    state = parse_qs(gh.query)["state"][0]
    return await client.get("/auth/callback", params={"code": github_code, "state": state})


def csrf_of(page: str) -> str:
    match = re.search(r"name=csrf value='([0-9a-f]{64})'", page)
    assert match, "no CSRF field on page"
    return match.group(1)


async def post(client: httpx.AsyncClient, csrf: str, **fields: str) -> httpx.Response:
    return await client.post("/admin", data={"csrf": csrf, **fields})


def is_signed_out(resp: httpx.Response) -> bool:
    """The sign-in page, not the admin page (which always carries a CSRF field)."""
    page = resp.text
    return resp.status_code == 200 and "href='/admin/login'" in page and "csrf" not in page


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> str:
    """Log alice in; returns a CSRF token for her session."""
    resp = await admin_login(client)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/admin")
    page = await client.get("/admin")
    assert page.status_code == 200
    return csrf_of(page.text)


# ----------------------------------------------------------------------
# who gets in


async def test_admin_page_requires_login(client: httpx.AsyncClient) -> None:
    # A sign-in button, not a bounce to GitHub (which would re-approve silently).
    assert is_signed_out(await client.get("/admin"))


async def test_session_cookie_attributes(client: httpx.AsyncClient) -> None:
    resp = await admin_login(client)
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attr in ("HttpOnly", "Path=/admin", "SameSite=lax"):
        assert attr.lower() in cookie.lower()


async def test_member_and_stranger_cannot_be_admin(
    server: ModuleType, client: httpx.AsyncClient
) -> None:
    from notes_mcp.members import GitHubUser

    server.members.add_member("bob", GitHubUser(2, "bob"))
    assert (await admin_login(client, "bob-code")).status_code == 403
    assert (await admin_login(client, "mallory-code")).status_code == 403
    assert is_signed_out(await client.get("/admin"))


async def test_mcp_token_does_not_open_admin(client: httpx.AsyncClient) -> None:
    token = await connect_github(client, "alice-code")
    resp = await client.get("/admin", headers={"Authorization": f"Bearer {token['access_token']}"})
    assert is_signed_out(resp)  # bearer tokens are ignored here
    resp = await client.post(
        "/admin",
        headers={"Authorization": f"Bearer {token['access_token']}"},
        data={"op": "add_member", "name": "eve"},
    )
    assert resp.status_code == 403


async def test_dropped_admin_session_dies(
    server: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient
) -> None:
    await admin_login(client)
    cookie = client.cookies.get(SESSION_COOKIE)
    reborn = build_server(repo, monkeypatch, admins="someone-else")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reborn.mcp.streamable_http_app()),
        base_url="http://localhost:8000",
        cookies={SESSION_COOKIE: cookie or ""},
    ) as c:
        assert is_signed_out(await c.get("/admin"))


# ----------------------------------------------------------------------
# request integrity


async def test_csrf_required(client: httpx.AsyncClient, admin: str) -> None:
    resp = await client.post("/admin", data={"op": "add_member", "name": "eve"})
    assert resp.status_code == 403
    resp = await post(client, "0" * 64, op="add_member", name="eve")
    assert resp.status_code == 403


async def test_cross_origin_post_refused(client: httpx.AsyncClient, admin: str) -> None:
    resp = await client.post(
        "/admin",
        data={"csrf": admin, "op": "add_member", "name": "eve"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


async def test_page_is_locked_down(client: httpx.AsyncClient, admin: str) -> None:
    page = await client.get("/admin")
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "form-action 'self'" in csp
    assert page.headers["cache-control"] == "no-store"
    assert "<script" not in page.text


async def test_names_are_escaped(server: ModuleType, client: httpx.AsyncClient, admin: str) -> None:
    server.members.add_member("ann")
    resp = await post(client, admin, op="set_grant", name="ann", path="<b>x", mode="ro")
    assert resp.status_code == 400  # invalid pattern, and the echo is escaped
    assert "<b>x" not in resp.text


# ----------------------------------------------------------------------
# operations


async def test_member_lifecycle(
    server: ModuleType, app: Any, client: httpx.AsyncClient, admin: str
) -> None:
    resp = await post(client, admin, op="add_member", name="ann")
    assert resp.status_code == 200 and "Added ann" in resp.text

    resp = await post(client, admin, op="set_grant", name="ann", path="kitchen/**", mode="rw")
    assert "kitchen/**" in resp.text
    resp = await post(client, admin, op="set_grant", name="ann", path="recipes", mode="ro")
    assert [(g.path, g.mode) for g in server.members.members()[0].grants] == [
        ("kitchen/**", "rw"),
        ("recipes", "ro"),
    ]
    # Changing a grant's level replaces it.
    await post(client, admin, op="set_grant", name="ann", path="recipes", mode="rw")
    assert [g.mode for g in server.members.members()[0].grants] == ["rw", "rw"]
    await post(client, admin, op="remove_grant", name="ann", path="recipes")
    assert [g.path for g in server.members.members()[0].grants] == ["kitchen/**"]

    resp = await post(client, admin, op="new_code", name="ann")
    code = re.search(r"<div class=code>(nm_[A-Za-z0-9_-]+)</div>", resp.text)
    assert code, "code not shown"
    page = await client.get("/admin")
    assert code.group(1) not in page.text  # shown once only

    token = await connect_code(client, code.group(1))
    async with app.router.lifespan_context(app):
        assert await mcp_status(client, token["access_token"]) == 200
        await post(client, admin, op="revoke", name="ann")
        assert await mcp_status(client, token["access_token"]) == 401

    await post(client, admin, op="remove_code", name="ann")
    assert server.members.login_code(code.group(1)) is None
    await post(client, admin, op="delete_member", name="ann")
    assert server.members.members() == []


async def test_github_member_via_admin(
    server: ModuleType, client: httpx.AsyncClient, admin: str
) -> None:
    resp = await post(client, admin, op="add_member", name="bob", github="bob")
    assert resp.status_code == 200
    member = server.members.members()[0]
    assert (member.github_id, member.github_login) == (2, "bob")
    resp = await post(client, admin, op="set_github", name="bob", github="nobody-here")
    assert resp.status_code == 400 and "no user" in resp.text
    resp = await post(client, admin, op="add_member", name="bob2", github="bob")
    assert resp.status_code == 400 and "already member bob" in resp.text
    resp = await post(client, admin, op="add_member", name="al", github="alice")
    assert resp.status_code == 400  # admins aren't members


async def test_grant_suggestions_list_repo_folders(client: httpx.AsyncClient, admin: str) -> None:
    page = await client.get("/admin")
    assert "<option value='bikepacking-gear'>" in page.text
    assert "<option value='resources'>" in page.text


async def test_logout(client: httpx.AsyncClient, admin: str) -> None:
    resp = await post(client, admin, op="logout")
    assert resp.status_code == 303 and resp.headers["location"] == "/admin?signed_out"
    page = await client.get(resp.headers["location"])
    assert is_signed_out(page) and "Signed out" in page.text
    assert (await admin_login(client)).status_code == 303  # can sign back in
    assert "Sign out" in (await client.get("/admin")).text


async def test_logout_ends_copied_sessions_but_not_claude(
    app: Any, server: ModuleType, client: httpx.AsyncClient
) -> None:
    """Sign-out is server-side: a copied cookie dies too. Claude stays connected."""
    token = await connect_github(client, "alice-code")
    await admin_login(client)
    stolen = client.cookies[SESSION_COOKIE]
    csrf = csrf_of((await client.get("/admin")).text)
    await post(client, csrf, op="logout")

    client.cookies.set(SESSION_COOKIE, stolen)
    assert is_signed_out(await client.get("/admin"))
    assert (await post(client, csrf, op="add_member", name="eve")).status_code == 403
    assert server.members.members() == []
    async with app.router.lifespan_context(app):
        assert await mcp_status(client, token["access_token"]) == 200


async def test_member_session_token_is_not_an_admin_session(server: ModuleType) -> None:
    """Even a validly signed admin_session for a member subject is refused."""
    server.members.add_member("ann")
    forged = server.auth_provider._mint("admin_session", "admin", "member:ann", 3600)
    assert server.auth_provider.verify_admin_session(forged) is None
    from notes_mcp.members import GitHubUser

    server.members.login_github(GitHubUser(1, "alice"))
    real = server.auth_provider._mint("admin_session", "admin", "admin:1", 3600)
    assert server.auth_provider.verify_admin_session(real) == "admin:1"


async def test_failed_admin_login_offers_a_way_back(client: httpx.AsyncClient) -> None:
    resp = await client.get("/admin/login")
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    refused = await client.get("/auth/callback", params={"code": "mallory-code", "state": state})
    assert refused.status_code == 403 and "href='/admin/login'" in refused.text
    # The link was spent by the refused attempt; going back can't reuse it.
    reused = await client.get("/auth/callback", params={"code": "alice-code", "state": state})
    assert reused.status_code == 400 and "already used" in reused.text
    assert "href='/admin/login'" in reused.text
    assert (await admin_login(client)).status_code == 303


async def test_referrer_policy_keeps_same_origin_posts_working(
    client: httpx.AsyncClient, admin: str
) -> None:
    # "no-referrer" makes browsers send Origin: null, which the origin check refuses.
    page = await client.get("/admin")
    assert page.headers["referrer-policy"] == "same-origin"
    resp = await client.post(
        "/admin",
        data={"csrf": admin, "op": "add_member", "name": "eve"},
        headers={"Origin": "http://localhost"},  # the test issuer
    )
    assert resp.status_code == 200
    resp = await client.post(
        "/admin",
        data={"csrf": admin, "op": "add_member", "name": "eve2"},
        headers={"Origin": "null"},
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# folder picker


@pytest.fixture
def folders(repo: Path) -> None:
    for note_id in ("kitchen/pantry", "kitchen/spices", "family/birthdays"):
        make_note(repo, note_id)


async def test_picker_shows_tree_and_saves(
    server: ModuleType, client: httpx.AsyncClient, admin: str, folders: None
) -> None:
    server.members.add_member("ann")
    server.members.set_grant("ann", "kitchen/**", "rw")  # same as "kitchen"
    page = (await post(client, admin, op="set_grant", name="ann", path="family", mode="ro")).text
    # The member just edited stays expanded; existing grants are pre-selected.
    assert "<details class=access open>" in page
    assert "name='g:family' value=ro checked" in page
    assert "name='g:bikepacking-gear' value=none checked" in page

    resp = await post(
        client,
        admin,
        op="set_access",
        name="ann",
        **{
            "g:family": "none",
            "g:kitchen": "ro",
            "g:resources": "ro",
            "g:bikepacking-gear": "none",
        },
    )
    assert resp.status_code == 200 and "access saved" in resp.text
    grants = [(g.path, g.mode) for g in server.members.members()[0].grants]
    assert grants == [("kitchen", "ro"), ("resources", "ro")]  # kitchen/** replaced
    resp = await post(client, admin, op="set_access", name="ann", **{"g:kitchen": "ro"})
    assert "no changes" in resp.text


async def test_picker_shows_inherited_access(
    server: ModuleType, client: httpx.AsyncClient, admin: str, folders: None
) -> None:
    server.members.add_member("ann")
    server.members.set_grant("ann", "kitchen", "rw")
    page = (await client.get("/admin")).text
    assert page.count("has read+write via kitchen") == 2  # pantry and spices
    # A granted folder stays collapsed; one with a grant inside opens.
    assert "<details><summary><span class=node><span><span class=granted>kitchen" in page
    server.members.set_grant("ann", "family/birthdays", "ro")
    page = (await client.get("/admin")).text
    assert "<details open><summary><span class=node><span><span>family" in page


async def test_picker_keeps_patterns_it_cannot_show(
    server: ModuleType, client: httpx.AsyncClient, admin: str
) -> None:
    server.members.add_member("ann")
    server.members.set_grant("ann", "*/recipes", "ro")
    page = (await client.get("/admin")).text
    assert "Other grants" in page and "*/recipes" in page
    await post(client, admin, op="set_access", name="ann", **{"g:resources": "rw"})
    assert {g.path for g in server.members.members()[0].grants} == {"*/recipes", "resources"}


async def test_picker_rejects_bad_input(
    server: ModuleType, client: httpx.AsyncClient, admin: str
) -> None:
    server.members.add_member("ann")
    for field, mode in (("g:kitchen", "admin"), ("g:../etc", "ro"), ("g:", "ro")):
        resp = await post(client, admin, op="set_access", name="ann", **{field: mode})
        assert resp.status_code == 400, field
    assert server.members.members()[0].grants == []
    resp = await post(client, admin, op="set_access", name="nobody", **{"g:kitchen": "ro"})
    assert resp.status_code == 400
