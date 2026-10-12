"""Browser pages: the Claude login page and the admin page.

The login page (/auth/login) is where /authorize sends people: sign in with
GitHub, or enter an access code. The admin page (/admin) is where ADMIN_USERS
manage members: add/remove them, link a GitHub account, mint/replace/remove
access codes, revoke sessions, and grant paths read-only or read-write.

Granting access is deliberately not an MCP tool: the model can be steered by
the content it reads, so who-may-see-what changes only through this page.

Admin page security:
- Sessions are a cookie (HttpOnly, SameSite=Lax, Secure on https, Path=/admin)
  holding an admin_session JWT, re-checked against ADMIN_USERS on every
  request. MCP bearer tokens are never accepted here.
- Every POST carries a CSRF token bound to the session, and a cross-origin
  Origin header is refused.
- Plain server-rendered HTML; no scripts; CSP forbids everything but inline
  styles; responses are no-store and can't be framed.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from html import escape
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from notes_mcp.access import Grants
from notes_mcp.auth import ADMIN_SESSION_TTL, CallbackError, NotesOAuthProvider
from notes_mcp.members import MODES, Member, MemberError, MemberStore

SESSION_COOKIE = "notes_admin"
GRANT_OPS = {"set_access", "set_grant", "remove_grant", "add_member"}
MAX_PATHS = 2000  # entries in the folder picker
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

# Palette from johnhringiv.com (parchment, green, rust links); component
# shapes after gotflashes. OKLCH tokens, dark variant from the same hues.
_CSS = """
:root{--bg:oklch(97% .025 90);--card:oklch(94% .035 85);--inset:oklch(90% .04 80);
--text:oklch(35% .04 75);--muted:oklch(48% .02 75);--line:oklch(50% .03 75 / .2);
--primary:oklch(60% .17 155);--primary-hover:oklch(55% .15 155);--on-primary:#fff;
--ro:oklch(85% .07 155);--link:oklch(48% .14 50);--danger:oklch(52% .19 27);
--field:oklch(99% .01 90);--code:oklch(92% .06 95);--radius:.375rem;--radius-lg:.5rem;
--shadow:0 .125rem .25rem oklch(0% 0 0 / .075)}
@media (prefers-color-scheme:dark){:root{--bg:oklch(22% .015 75);--card:oklch(27% .02 75);
--inset:oklch(31% .022 75);--text:oklch(90% .02 85);--muted:oklch(70% .02 80);
--line:oklch(80% .02 80 / .15);--ro:oklch(42% .08 155);--link:oklch(72% .13 55);
--danger:oklch(68% .17 27);--field:oklch(24% .015 75);--code:oklch(33% .04 95)}}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;font:1rem/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
color:var(--text);background:var(--bg);-webkit-font-smoothing:antialiased}
a{color:var(--link)}
.navbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
gap:.5rem 1rem;background:var(--primary);color:var(--on-primary);padding:.6rem 1.5rem}
.brand{font-size:1.25rem;color:var(--on-primary);text-decoration:none}
.navbar .who{font-size:.9rem;display:flex;align-items:center;gap:.6rem}
main{max-width:960px;margin:0 auto;padding:1.5rem 16px 3rem}
h1,h2,h3{font-weight:500;line-height:1.2;margin:0 0 .5rem}
h1{font-size:2rem}h3{font-size:1.25rem}
h2{font-size:1.5rem;margin-top:2rem;padding-bottom:.25rem;display:inline-block;
border-bottom:2px solid var(--primary)}
.card{background:var(--card);border-radius:var(--radius-lg);padding:1rem 1.25rem;
margin:1rem 0}
.card h3{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap}
.meta{font-size:.9rem;color:var(--muted);font-weight:400}
.muted{color:var(--muted)}.row{margin:.6rem 0}
.actions{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin-top:.75rem}
form{display:inline-flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:0}
form.picker{display:block}
input,select,button{font:inherit;font-size:.9rem}
input:not([type]),input[type=text],select{height:2.25rem;padding:0 .7rem;
color:var(--text);background:var(--field);border:1px solid var(--line);
border-radius:var(--radius)}
input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid
var(--primary);outline-offset:1px}
button,a.btn{--btn:var(--inset);--fg:var(--text);display:inline-flex;align-items:center;
height:2.25rem;padding:0 .9rem;font-weight:600;font-size:.9rem;white-space:nowrap;
color:var(--fg);background:var(--btn);text-decoration:none;cursor:pointer;
border:1px solid color-mix(in oklab,var(--btn),#000 8%);border-radius:var(--radius);
box-shadow:var(--shadow);transition:background-color .15s}
button:hover,a.btn:hover{background:color-mix(in oklab,var(--btn),#000 7%)}
.primary,a.btn.primary{--btn:var(--primary);--fg:var(--on-primary)}
.danger{--btn:transparent;--fg:var(--danger);border-color:var(--danger);box-shadow:none}
.danger:hover{--btn:var(--danger);--fg:var(--on-primary);background:var(--danger)}
.navbar button{--btn:transparent;--fg:var(--on-primary);border-color:oklch(100% 0 0 / .5);
box-shadow:none;height:1.9rem}
.alert{border-radius:var(--radius-lg);padding:.75rem 1rem;margin:1rem 0;
border-left:4px solid var(--primary);background:var(--card)}
.alert.err{border-left-color:var(--danger)}
.code{font:1.1rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
background:var(--code);border-radius:var(--radius);padding:.6rem .8rem;margin-top:.5rem;
word-break:break-all;user-select:all}
table{border-collapse:collapse;width:100%}td{padding:.35rem .5rem;
border-bottom:1px solid var(--line);vertical-align:middle}
.badge{display:inline-block;padding:.1rem .5rem;margin:0 .25rem .25rem 0;font-size:.8rem;
border-radius:var(--radius);background:var(--ro);color:var(--text)}
.badge.rw{background:var(--primary);color:var(--on-primary)}
details.access{background:var(--inset);border-radius:var(--radius-lg);padding:.5rem .75rem;
margin-top:.75rem;box-shadow:var(--shadow)}
details.access>summary{cursor:pointer;font-weight:500;padding:.2rem 0}
details.access[open]>summary{margin-bottom:.5rem}
details.more>summary{cursor:pointer;color:var(--muted);font-size:.9rem;margin-top:.5rem}
ul.tree{list-style:none;margin:0;padding-left:1.1rem}
ul.tree.root{padding-left:0}
.node{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;
gap:.25rem 1rem;padding:.2rem .4rem;border-radius:var(--radius)}
.node:hover{background:color-mix(in oklab,var(--card),var(--text) 6%)}
li>details>summary{list-style-position:outside;cursor:pointer;margin-left:1rem}
li>details>summary .node{display:inline-flex;width:100%;vertical-align:middle}
.leaf{margin-left:1rem}
.granted{font-weight:600}
.via{font-size:.8rem;color:var(--muted);margin-left:.4rem}
.pick{margin-left:auto}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--radius);
overflow:hidden;background:var(--field);font-size:.8rem}
.seg label{position:relative;padding:.1rem .6rem;cursor:pointer;
border-left:1px solid var(--line)}
.seg label:first-child{border-left:0}
.seg input{position:absolute;opacity:0;inset:0;margin:0;cursor:pointer}
.seg label:has(input:checked){background:color-mix(in oklab,var(--inset),var(--text) 12%);
font-weight:600}
.seg label:has(input[value=ro]:checked){background:var(--ro)}
.seg label:has(input[value=rw]:checked){background:var(--primary);color:var(--on-primary)}
.seg label:has(input:focus-visible){outline:2px solid var(--primary);outline-offset:-2px}
.save{position:sticky;bottom:0;background:var(--inset);padding:.5rem 0}
ul.plain{margin:.25rem 0;padding-left:1.25rem}
@media (max-width:560px){.navbar{padding:.6rem 16px}h1{font-size:1.6rem}}
"""


def _page(
    title: str, body: str, *, status: int = 200, admin: bool = False, nav: str = ""
) -> HTMLResponse:
    csp = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    if admin:
        # Not on the login page: the code form's response redirects to the
        # MCP client, and browsers apply form-action to redirects.
        csp += "; form-action 'self'"
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<header class=navbar><span class=brand>notes-mcp</span>{nav}</header>"
        f"<main>{body}</main></body></html>",
        status_code=status,
        headers={
            "Content-Security-Policy": csp,
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            # Not no-referrer: browsers then send "Origin: null" on our own
            # form posts, and the admin page's cross-origin check refuses them.
            "Referrer-Policy": "same-origin",
        },
    )


# Sign-in links are single-use, so a failed attempt needs a way to start over.
START_AGAIN = (
    "<p>Managing members? <a class='btn primary' href='/admin/login'>Sign in to the admin "
    "page</a></p>"
    "<p class=muted>Connecting Claude? Start again from the connector in Claude's settings.</p>"
)


def error_page(message: str, status: int, start_again: bool = False) -> HTMLResponse:
    return _page(
        "Sign-in problem",
        f"<h1>Sign-in problem</h1><div class='alert err'>{escape(message)}</div>"
        + (START_AGAIN if start_again else ""),
        status=status,
    )


def signed_out_page(signed_out: bool) -> HTMLResponse:
    """Where /admin lands without a session. Not an automatic bounce to GitHub:
    GitHub approves an app you've used before without asking, so signing out
    would land you straight back in."""
    if signed_out:
        head = "<h1>Signed out</h1><div class=alert>You're signed out of the admin page.</div>"
        note = (
            "<p class=muted>You may still be signed in to GitHub itself; on a shared "
            "computer, sign out at github.com too.</p>"
        )
    else:
        head, note = "<h1>Admin</h1>", ""
    return _page(
        "Admin sign-in",
        f"""{head}<div class=card><h3>Manage who can use your notes</h3>
<a class='btn primary' href='/admin/login'>Sign in with GitHub</a></div>{note}""",
    )


def login_page(state: str, message: str | None = None, status: int = 200) -> HTMLResponse:
    err = f"<div class='alert err'>{escape(message)}</div>" if message else ""
    s = escape(state, quote=True)
    return _page(
        "Connect Claude to notes",
        f"""<h1>Connect Claude to your notes</h1>{err}
<div class=card><h3>Have a GitHub account the owner added?</h3>
<a class='btn primary' href="/auth/github?state={s}">Sign in with GitHub</a></div>
<div class=card><h3>Or use the access code you were given</h3>
<form method=post action="/auth/code"><input type=hidden name=state value="{s}">
<input name=code size=36 autocomplete=off required aria-label="Access code"
placeholder="nm_…"><button class=primary>Continue</button></form></div>""",
        status=status,
    )


def grantable_paths(note_ids: list[str], resource_ids: list[str]) -> list[str]:
    """Folders and notes to suggest in the grant form (any pattern is accepted)."""
    choices: set[str] = {"resources"}
    for note_id in note_ids:
        parts = note_id.split("/")
        choices.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    for rid in resource_ids:
        parts = rid.split("/")
        choices.update("resources/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(choices)[:MAX_PATHS]


_LABELS = {"none": "none", "ro": "read", "rw": "read+write"}
_RANK = {"none": 0, "ro": 1, "rw": 2}


def _own_modes(member: Member) -> dict[str, str]:
    """Grants by the path they name ("kitchen/**" is shown as "kitchen")."""
    return {g.path.removesuffix("/**"): g.mode for g in member.grants}


def _effective(grants: Grants, path: str) -> str:
    return "rw" if grants.can_write(path) else "ro" if grants.can_read(path) else "none"


def _source(member: Member, path: str, mode: str) -> str:
    """Which other grant gives `path` its `mode` (for the "via" hint)."""
    for g in member.grants:
        if g.path.removesuffix("/**") == path:
            continue
        single = Grants.build(
            read=[g.path] if g.mode == "ro" else [], write=[g.path] * (g.mode == "rw")
        )
        if _effective(single, path) == mode:
            return g.path
    return "?"


def picker_tree(member: Member, paths: list[str], radio: Callable[[str, str], str]) -> str:
    """Nested folder list, each entry with none/read/read+write radios.

    No scripts: folders are <details>, open where something inside is granted.
    """
    own = _own_modes(member)
    grants = member.build_grants()
    children: dict[str, list[str]] = {}
    for path in paths:
        parent = path.rpartition("/")[0]
        children.setdefault(parent, []).append(path)

    def has_grant_below(path: str) -> bool:
        return any(p.startswith(path + "/") for p in own)

    def node(path: str) -> str:
        mine = own.get(path, "none")
        eff = _effective(grants, path)
        via = (
            f" <span class=via>has {_LABELS[eff]} via {escape(_source(member, path, eff))}</span>"
            if _RANK[eff] > _RANK[mine]
            else ""
        )
        cls = " class=granted" if mine != "none" else ""
        label = escape(path.rpartition("/")[2])
        return (
            f"<span class=node><span><span{cls}>{label}</span>{via}</span>"
            f"<span class=pick>{radio(path, mine)}</span></span>"
        )

    def items(parent: str) -> str:
        out = []
        for path in children.get(parent, []):
            if path in children:
                opened = " open" if has_grant_below(path) else ""
                out.append(
                    f"<li><details{opened}><summary>{node(path)}</summary>"
                    f"<ul class=tree>{items(path)}</ul></details></li>"
                )
            else:
                out.append(f"<li class=leaf>{node(path)}</li>")
        return "".join(out)

    return f"<ul class='tree root'>{items('')}</ul>"


def register(
    mcp: FastMCP,
    provider: NotesOAuthProvider | None,
    members: MemberStore,
    paths: Callable[[], list[str]],
) -> None:
    """Mount the login and admin routes. `paths` lists grantable paths."""

    secure = provider is not None and provider.issuer_url.startswith("https://")

    def disabled() -> Response:
        return error_page("Sign-in is disabled on this server (PUBLIC_URL not set).", 404)

    # ------------------------------------------------------------------
    # Claude login

    @mcp.custom_route("/auth/login", methods=["GET"])  # type: ignore[untyped-decorator]
    async def auth_login(request: Request) -> Response:
        if provider is None:
            return disabled()
        state = request.query_params.get("state", "")
        if not provider.login_page_ok(state):
            return error_page("This sign-in link expired. Start again from Claude.", 400)
        return login_page(state)

    @mcp.custom_route("/auth/github", methods=["GET"])  # type: ignore[untyped-decorator]
    async def auth_github(request: Request) -> Response:
        if provider is None:
            return disabled()
        try:
            url = provider.github_url_for(request.query_params.get("state"))
        except CallbackError as exc:
            return error_page(f"{exc} Start again from Claude.", exc.status)
        return RedirectResponse(url, status_code=302)

    @mcp.custom_route("/auth/code", methods=["POST"])  # type: ignore[untyped-decorator]
    async def auth_code(request: Request) -> Response:
        if provider is None:
            return disabled()
        form = await request.form()
        state, code = str(form.get("state", "")), str(form.get("code", ""))
        try:
            url = provider.handle_code_login(state, code)
        except CallbackError as exc:
            if provider.login_page_ok(state):
                return login_page(state, str(exc), status=exc.status)
            return error_page(f"{exc} Too many attempts; start again from Claude.", exc.status)
        return RedirectResponse(url, status_code=302)

    @mcp.custom_route("/auth/callback", methods=["GET"])  # type: ignore[untyped-decorator]
    async def auth_callback(request: Request) -> Response:
        """GitHub redirects here; finish a Claude login or an admin login."""
        if provider is None:
            return disabled()
        try:
            result = await provider.handle_callback(
                request.query_params.get("code"), request.query_params.get("state")
            )
        except CallbackError as exc:
            return error_page(str(exc), exc.status, start_again=True)
        if result.admin_session is None:
            return RedirectResponse(result.redirect_url, status_code=302)
        response = RedirectResponse(result.redirect_url, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            result.admin_session,
            max_age=ADMIN_SESSION_TTL,
            path="/admin",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response

    # ------------------------------------------------------------------
    # admin

    @mcp.custom_route("/admin/login", methods=["GET"])  # type: ignore[untyped-decorator]
    async def admin_login(request: Request) -> Response:
        if provider is None:
            return disabled()
        return RedirectResponse(provider.begin_admin_login(), status_code=302)

    def session_of(request: Request) -> tuple[str, str] | None:
        assert provider is not None
        token = request.cookies.get(SESSION_COOKIE)
        subject = provider.verify_admin_session(token)
        return (subject, token) if subject and token else None

    @mcp.custom_route("/admin", methods=["GET"])  # type: ignore[untyped-decorator]
    async def admin_get(request: Request) -> Response:
        if provider is None:
            return disabled()
        session = session_of(request)
        if session is None:
            return signed_out_page("signed_out" in request.query_params)
        return render(session)

    @mcp.custom_route("/admin", methods=["POST"])  # type: ignore[untyped-decorator]
    async def admin_post(request: Request) -> Response:
        if provider is None:
            return disabled()
        session = session_of(request)
        if session is None:
            return error_page("Your admin session ended.", 403, start_again=True)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != urlparse(provider.issuer_url).netloc:
            return error_page("Cross-origin request refused.", 403)
        form = await request.form(max_fields=MAX_PATHS + 100)
        sent = str(form.get("csrf", ""))
        if not hmac.compare_digest(sent, provider.csrf_token(session[1])):
            return error_page("Form expired or forged (CSRF check failed). Reload /admin.", 403)
        fields = {k: str(v) for k, v in form.items()}
        if fields.get("op") == "logout":
            # Ends this admin's sessions everywhere, not just this cookie.
            members.end_admin_sessions(session[0])
            response = RedirectResponse("/admin?signed_out", status_code=303)
            response.delete_cookie(SESSION_COOKIE, path="/admin")
            return response
        # Keep the member being edited expanded after the round trip.
        open_member = fields.get("name") if fields.get("op") in GRANT_OPS else None
        try:
            message, code = await apply(fields)
        except MemberError as exc:
            return render(session, error=str(exc), open_member=open_member, status=400)
        return render(session, message=message, code=code, open_member=open_member)

    async def resolve_github(login: str) -> Any:
        assert provider is not None
        login = login.strip().lstrip("@")
        if not _GITHUB_LOGIN_RE.match(login):
            raise MemberError(f"{login!r} is not a valid GitHub username")
        user = await provider.lookup(login)
        if user is None:
            raise MemberError(f"GitHub has no user {login!r}")
        return user

    async def apply(f: dict[str, str]) -> tuple[str, tuple[str, str] | None]:
        op, name = f.get("op", ""), f.get("name", "")
        match op:
            case "add_member":
                github = await resolve_github(f["github"]) if f.get("github", "").strip() else None
                member = members.add_member(name, github)
                return f"Added {member.name}. Grant paths, then give them a way to sign in.", None
            case "delete_member":
                members.delete_member(name)
                return f"Deleted {name}; their sessions ended.", None
            case "set_github":
                github = await resolve_github(f["github"]) if f.get("github", "").strip() else None
                members.set_github(name, github)
                linked = f"linked to GitHub {github.login}" if github else "unlinked from GitHub"
                return f"{name} {linked}; existing sessions ended.", None
            case "new_code":
                return f"New access code for {name}; old code and sessions ended.", (
                    name,
                    members.new_code(name),
                )
            case "remove_code":
                members.remove_code(name)
                return f"Removed {name}'s access code; their sessions ended.", None
            case "revoke":
                members.revoke_sessions(name)
                return f"Ended all of {name}'s sessions.", None
            case "set_grant":
                members.set_grant(name, f.get("path", ""), f.get("mode", ""))
                return f"{name}: {f.get('path', '').strip()} is {f.get('mode')}.", None
            case "set_access":
                picked = {k[2:]: v for k, v in f.items() if k.startswith("g:")}
                changed = members.set_grants(name, picked)
                return (
                    f"{name}: access saved; applies on their next request."
                    if changed
                    else f"{name}: no changes.",
                    None,
                )
            case "remove_grant":
                members.remove_grant(name, f.get("path", ""))
                return f"{name}: removed {f.get('path')}.", None
        raise MemberError(f"unknown action {op!r}")

    # ------------------------------------------------------------------
    # rendering

    def render(
        session: tuple[str, str],
        *,
        message: str | None = None,
        error: str | None = None,
        code: tuple[str, str] | None = None,
        open_member: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        assert provider is not None
        csrf = escape(provider.csrf_token(session[1]), quote=True)
        principal = members.resolve(session[0])
        me = escape(principal.name if principal else "?")

        def form(op: str, inner: str, label: str, name: str = "", cls: str = "") -> str:
            n = f"<input type=hidden name=name value='{escape(name, quote=True)}'>" if name else ""
            c = f" class={cls}" if cls else ""
            return (
                f"<form method=post action='/admin'><input type=hidden name=csrf value='{csrf}'>"
                f"<input type=hidden name=op value={op}>{n}{inner}<button{c}>{label}</button>"
                "</form>"
            )

        def mode_select(current: str = "ro") -> str:
            opts = "".join(
                f"<option value={m}{' selected' if m == current else ''}>"
                f"{'read-only' if m == 'ro' else 'read-write'}</option>"
                for m in MODES
            )
            return f"<select name=mode>{opts}</select>"

        def radio(path: str, mine: str) -> str:
            field = escape(f"g:{path}", quote=True)
            return (
                "<span class=seg>"
                + "".join(
                    f"<label><input type=radio name='{field}' value={mode}"
                    f"{' checked' if mode == mine else ''}>{label}</label>"
                    for mode, label in _LABELS.items()
                )
                + "</span>"
            )

        tree_paths = paths()
        in_tree = set(tree_paths)

        def access_html(m: Member) -> str:
            n = m.name
            granted = (
                "".join(
                    f"<span class='badge {g.mode}' title='{_LABELS[g.mode]}'>{escape(g.path)}"
                    f"{' · read' if g.mode == 'ro' else ' · read+write'}</span>"
                    for g in m.grants
                )
                or "<span class=muted>no access yet</span>"
            )
            opened = " open" if n == open_member else ""
            picker = (
                f"<form class=picker method=post action='/admin'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=hidden name=op value=set_access>"
                f"<input type=hidden name=name value='{escape(n, quote=True)}'>"
                f"{picker_tree(m, tree_paths, radio)}"
                f"<div class=save><button class=primary>Save access</button> <span class=muted>"
                f"A folder's access covers everything inside it.</span></div></form>"
            )
            # Grants the tree can't show: patterns, or paths that no longer exist.
            others = [g for g in m.grants if g.path.removesuffix("/**") not in in_tree]
            other_rows = "".join(
                f"<tr><td><code>{escape(g.path)}</code></td><td>{_LABELS[g.mode]}</td><td>"
                + form(
                    "remove_grant",
                    f"<input type=hidden name=path value='{escape(g.path, quote=True)}'>",
                    "Remove",
                    n,
                )
                + "</td></tr>"
                for g in others
            )
            other_html = (
                "<p>Other grants (patterns, or paths not in the repo):</p>"
                f"<table>{other_rows}</table>"
                if others
                else ""
            )
            pattern = form(
                "set_grant",
                "<input name=path list=paths placeholder='e.g. kitchen/*/recipes' required> "
                + mode_select(),
                "Grant",
                n,
            )
            return (
                f"<details class=access{opened}><summary>Access {granted}</summary>{picker}"
                f"{other_html}<details class=more><summary>Grant a glob pattern</summary>"
                f"<div class=row>{pattern}</div></details></details>"
            )

        def member_html(m: Member) -> str:
            n = m.name
            gh = escape(m.github_login) if m.github_login else "<span class=muted>none</span>"
            code_state = "set" if m.code_hash else "<span class=muted>none</span>"
            code_btn = "Replace code" if m.code_hash else "Create code"
            last = m.last_login[:16].replace("T", " ") if m.last_login else "never"
            return f"""<div class=card><h3>{escape(n)}<span class=meta>added
{escape(m.created_at[:10])} &middot; last sign-in {escape(last)}</span></h3>
<div>GitHub: {gh} &middot; Access code: {code_state}</div>
{access_html(m)}
<div class=actions>{form("new_code", "", code_btn, n)}
{form("remove_code", "", "Remove code", n) if m.code_hash else ""}
{form("revoke", "", "End sessions", n)}
{form("set_github", "<input name=github placeholder='GitHub username' size=16> ", "Set GitHub", n)}
{form("delete_member", "", "Delete member", n, cls="danger")}</div></div>"""

        admins = "".join(
            f"<li>{escape(login)}"
            + (
                " <span class=muted>(pinned)</span>"
                if pin
                else " <span class=muted>(not yet signed in)</span>"
            )
            + "</li>"
            for login, pin in members.admins().items()
        )
        flash = ""
        if message:
            flash += f"<div class=alert>{escape(message)}</div>"
        if error:
            flash += f"<div class='alert err'>{escape(error)}</div>"
        if code:
            flash += (
                f"<div class=alert>Give this code to <b>{escape(code[0])}</b>. "
                f"It is shown only once.<div class=code>{escape(code[1])}</div></div>"
            )
        datalist = "".join(f"<option value='{escape(p, quote=True)}'>" for p in tree_paths)
        nav = f"<span class=who>Signed in as {me} {form('logout', '', 'Sign out')}</span>"
        body = f"""<h1>Who can use your notes</h1>{flash}
<h2>Members</h2>
{"".join(member_html(m) for m in members.members()) or "<p class=muted>No members yet.</p>"}
<div class=card><h3>Add a member</h3>{
            form(
                "add_member",
                "<input name=name placeholder='name (e.g. ann)' required pattern='[a-z0-9-]+'> "
                "<input name=github placeholder='GitHub username (optional)'> ",
                "Add",
                cls="primary",
            )
        }</div>
<h2>Admins</h2><div class=card><ul class=plain>{admins}</ul>
<p class=muted>Admins have full access and come from ADMIN_USERS in the server config.</p>
</div><datalist id=paths>{datalist}</datalist>"""
        return _page("notes-mcp admin", body, status=status, admin=True, nav=nav)
