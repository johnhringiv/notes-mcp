"""Run the server locally with a fake GitHub, for iterating on the login and admin pages.

    uv run python dev/serve.py            # http://localhost:8000/admin
    uv run python dev/serve.py --fresh    # throw away the scratch repo and members

Sign-in goes to a local "GitHub" page where you pick who to be: the admin
("dev") or any username you type. Adding a member by GitHub username on the
admin page always succeeds, with a made-up id. Everything else — sessions,
CSRF, grants, access codes, the MCP endpoint — is the real code.

The notes repo and server state live in a scratch directory (printed at
startup), never your real notes. Code changes under src/ reload the server.

This file is not part of the package or the Docker image.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from notes_mcp.auth import GitHubUpstream
from notes_mcp.members import GitHubUser

ADMIN = "dev"
SCRATCH = Path(tempfile.gettempdir()) / "notes-mcp-dev"

SEED_NOTES = {
    "kitchen/spice-inventory": "# Spice inventory\n\n- Cumin: half jar\n- Smoked paprika: full\n",
    "kitchen/pantry": "# Pantry\n\n- Rice\n- Lentils\n",
    "family/birthdays": "# Birthdays\n\n- Mom: March 3\n",
    "bikepacking-gear": "# Bikepacking gear\n\n- Tailfin rack\n",
    "private/journal": "# Journal\n\nNot for sharing.\n",
}
SEED_RESOURCES = {
    "resources/recipes/dal.md": "# Dal\n\nLentils, cumin, turmeric.\n",
    "resources/manuals/stove.md": "# Stove manual\n",
}


def _fake_id(login: str) -> int:
    # Stable per username so re-adding the same person matches.
    return 1000 + int(hashlib.sha256(login.lower().encode()).hexdigest()[:6], 16)


def _seed_repo(repo: Path) -> None:
    for note_id, body in SEED_NOTES.items():
        title = note_id.rsplit("/", 1)[-1].replace("-", " ").title()
        path = repo / note_id / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\ntags: []\ncreated: 2026-01-01\n---\n{body}")
    for rel, body in SEED_RESOURCES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("add", "-A")
    git("-c", "user.name=dev", "-c", "user.email=dev@localhost", "commit", "-m", "Seed")


def _picker(state: str) -> str:
    def button(login: str, label: str) -> str:
        query = urlencode({"code": login, "state": state})
        return f"<p><a href='/auth/callback?{html.escape(query)}'>{html.escape(label)}</a></p>"

    return (
        "<!doctype html><meta charset=utf-8><title>Fake GitHub</title>"
        "<style>body{font:16px system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem}"
        "a{display:block;padding:.6rem;border:1px solid #ccc;border-radius:6px;"
        "text-decoration:none}</style>"
        "<h1>Fake GitHub</h1><p>Dev server only. Sign in as:</p>"
        + button(ADMIN, f"{ADMIN} (admin)")
        + "<form action=/dev/github/as><input type=hidden name=state value='"
        + html.escape(state)
        + "'><input name=login placeholder='any username' required> "
        "<button>Sign in</button></form>"
    )


class _FakeUpstream(GitHubUpstream):
    def authorize_url(self, state: str) -> str:
        return f"{os.environ['PUBLIC_URL']}/dev/github?{urlencode({'state': state})}"


def create_app() -> Starlette:
    import notes_mcp.server as server

    provider = server.auth_provider
    assert provider is not None, "dev server needs auth on (PUBLIC_URL)"

    async def identify(code: str) -> GitHubUser:
        return GitHubUser(_fake_id(code), code)

    async def lookup(login: str) -> GitHubUser | None:
        return GitHubUser(_fake_id(login), login)

    provider.identify = identify
    provider.lookup = lookup
    provider.upstream.__class__ = _FakeUpstream

    @server.mcp.custom_route("/dev/github", methods=["GET"])  # type: ignore[untyped-decorator]
    async def fake_github(request: Request) -> Response:
        return HTMLResponse(_picker(request.query_params.get("state", "")))

    @server.mcp.custom_route("/dev/github/as", methods=["GET"])  # type: ignore[untyped-decorator]
    async def fake_github_as(request: Request) -> Response:
        login = request.query_params.get("login", "").strip()
        query = urlencode({"code": login, "state": request.query_params.get("state", "")})
        return RedirectResponse(f"/auth/callback?{query}", status_code=302)

    return server.mcp.streamable_http_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local dev server with a fake GitHub")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fresh", action="store_true", help="reset the scratch repo and state")
    args = parser.parse_args()

    if args.fresh and SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    repo, state = SCRATCH / "notes", SCRATCH / "state"
    if not repo.exists():
        repo.mkdir(parents=True)
        _seed_repo(repo)
    state.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "gh_secret").write_text("dev-secret")

    public_url = f"http://localhost:{args.port}"
    os.environ.update(
        NOTES_REPO_PATH=str(repo),
        OAUTH_STATE_DIR=str(state),
        PUBLIC_URL=public_url,
        GITHUB_OAUTH_CLIENT_ID="dev-client",
        GITHUB_OAUTH_CLIENT_SECRET_FILE=str(SCRATCH / "gh_secret"),
        ADMIN_USERS=ADMIN,
    )
    for var in ("GITHUB_ALLOWED_LOGIN", "NOTES_REPO_URL", "GITHUB_TOKEN"):
        os.environ.pop(var, None)

    print(f"scratch dir: {SCRATCH}")
    print(f"admin page:  {public_url}/admin   (sign in as '{ADMIN}')")
    print(f"MCP:         {public_url}/mcp")
    uvicorn.run(
        "serve:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        reload=True,
        reload_dirs=[str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")],
        app_dir=str(Path(__file__).parent),
    )


if __name__ == "__main__":
    main()
