"""FastMCP app and tool registration.

Read tools call the store directly. Write tools run through `_write_flow`:
pull --rebase → mutate → add/commit/push, serialized by one process-wide
asyncio lock so concurrent tool calls can't interleave git operations
(see PRD.md, git workflow). The blocking git/filesystem work runs in a worker
thread to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import anyio.to_thread
import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from notes_mcp import files, scripts
from notes_mcp.auth import SCOPES, CallbackError, GitHubOAuthProvider
from notes_mcp.config import Settings
from notes_mcp.git_ops import GitError, GitOps, write_flow
from notes_mcp.logging import logged_tool, logger, setup_logging
from notes_mcp.notes import NotesStore

settings = Settings.from_env()
store = NotesStore(settings.notes_repo_path)
git_ops = GitOps(
    repo_path=settings.notes_repo_path,
    branch=settings.notes_repo_branch,
    author_name=settings.git_author_name,
    author_email=settings.git_author_email,
    token=settings.github_token,
)

_write_lock = asyncio.Lock()


def _build_auth() -> tuple[GitHubOAuthProvider | None, AuthSettings | None]:
    if not settings.auth_enabled:
        return None, None
    assert settings.public_url and settings.github_oauth_client_id
    assert settings.github_oauth_client_secret and settings.github_allowed_login
    provider = GitHubOAuthProvider(
        issuer_url=settings.public_url,
        github_client_id=settings.github_oauth_client_id,
        github_client_secret=settings.github_oauth_client_secret,
        allowed_login=settings.github_allowed_login,
        state_dir=settings.oauth_state_dir,
    )
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(settings.public_url),
        resource_server_url=AnyHttpUrl(f"{settings.public_url}/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
        ),
        required_scopes=None,
    )
    return provider, auth


auth_provider, auth_settings = _build_auth()

mcp = FastMCP(
    "notes",
    host=settings.host,
    port=settings.port,
    auth_server_provider=auth_provider,
    auth=auth_settings,
)


async def _locked_write(
    mutate: Callable[[], dict[str, Any]],
    commit_paths: Callable[[dict[str, Any]], list[str]],
    message: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    async with _write_lock:
        return await anyio.to_thread.run_sync(
            write_flow, store, git_ops, mutate, commit_paths, message
        )


# ----------------------------------------------------------------------
# read tools


@mcp.tool()
@logged_tool
def list_notes(filter: str | None = None) -> dict[str, Any]:
    """List all notes as {id, title, tags, updated_at, path}, newest first.

    A note is either a bare markdown file (id like "ncc/parser_flow.md") or
    a folder containing index.md plus data files/scripts (id like
    "cycling-analysis"). `filter` is an optional substring matched
    (case-insensitively) against each note's id, title, and tags.
    """
    return store.list_notes(filter)


@mcp.tool()
@logged_tool
def read_note(note_id: str) -> dict[str, Any]:
    """Read a note: its index.md content, frontmatter, and sibling files.

    `note_id` is the note's folder name relative to the repo root, as
    returned by list_notes.
    """
    return store.read_note(note_id)


@mcp.tool()
@logged_tool
def search_notes(query: str, max_results: int = 20) -> dict[str, Any]:
    """Full-text search (ripgrep, literal, smart-case) across all notes.

    Returns matches as {note_id, file, line_number, snippet} with ±2 lines
    of context around each match. Binary files are skipped.
    """
    return store.search_notes(query, max_results)


# ----------------------------------------------------------------------
# write tools (git-wrapped)


@mcp.tool()
@logged_tool
async def create_note(
    note_id: str,
    title: str,
    tags: list[str] | None = None,
    template: str | None = None,
) -> dict[str, Any]:
    """Create a new note from a template.

    A `note_id` ending in .md (e.g. "ncc/new-idea.md") creates a single
    markdown file; otherwise a folder note with index.md is created (use
    those when the note needs data files or scripts). `template` names a
    file in the repo's .templates/ directory; omit it to use the built-in
    default. Fails if the note already exists.
    """
    return await _locked_write(
        lambda: store.create_note(note_id, title, tags, template),
        lambda _: [store.md_relpath(note_id)],
        lambda _: f"Create note: {note_id}",
    )


@mcp.tool()
@logged_tool
async def append_to_note(note_id: str, content: str, section: str | None = None) -> dict[str, Any]:
    """Append markdown content to a note's index.md.

    With `section`, the content is appended at the end of that heading's
    section (the heading is created at the end of the note if absent).
    Without it, content is appended to the end of the file.
    """
    return await _locked_write(
        lambda: store.append_to_note(note_id, content, section),
        lambda _: [store.md_relpath(note_id)],
        lambda _: f"Append to {note_id}",
    )


@mcp.tool()
@logged_tool
async def edit_note(note_id: str, old_str: str, new_str: str) -> dict[str, Any]:
    """Replace exactly one occurrence of old_str with new_str in index.md.

    Fails with a structured error if old_str matches zero or multiple
    times — include enough surrounding context to make it unique. For
    larger restructures, compose a sequence of these edits.
    """
    return await _locked_write(
        lambda: store.edit_note(note_id, old_str, new_str),
        lambda _: [store.md_relpath(note_id)],
        lambda _: f"Edit {note_id}",
    )


@mcp.tool()
@logged_tool
async def add_file_to_note(note_id: str, filename: str, content_b64: str) -> dict[str, Any]:
    """Save a file (base64-encoded content) into a note's folder.

    Handles text and binary uniformly — encode the raw bytes as base64.
    The filename must be a bare name (no path separators). Existing files
    are overwritten (the response says so).
    """
    return await _locked_write(
        lambda: files.add_file_to_note(store, note_id, filename, content_b64),
        lambda r: [f"{note_id}/{r['filename']}"],
        lambda r: f"Add {r['filename']} to {note_id}",
    )


@mcp.tool()
@logged_tool
def note_history(note_id: str, limit: int = 10) -> dict[str, Any]:
    """List the git commits that touched a note, newest first.

    Returns {history: [{commit, author, date, message}]}. Use a commit sha
    with restore_note to bring back an older version.
    """
    if err := store.check_note(note_id):
        return err
    path = note_id if not note_id.endswith(".md") else store.md_relpath(note_id)
    return {"history": git_ops.file_history(path, limit)}


@mcp.tool()
@logged_tool
def read_note_file(note_id: str, filename: str) -> dict[str, Any]:
    """Read an attached file from a folder note.

    `filename` is relative to the note folder as listed by read_note (e.g.
    "weights.csv" or "scripts/analyze.py"). Text comes back as utf-8
    `content`; binary as base64 `content_b64` (check `encoding`).
    """
    return files.read_note_file(store, note_id, filename)


@mcp.tool()
@logged_tool
async def write_note(note_id: str, content: str) -> dict[str, Any]:
    """Replace a note's entire markdown body.

    For restructures too large for edit_note chains. The old version stays
    in git history (see note_history / restore_note), so this is safe.
    """
    return await _locked_write(
        lambda: store.write_note(note_id, content),
        lambda _: [store.md_relpath(note_id)],
        lambda _: f"Rewrite {note_id}",
    )


@mcp.tool()
@logged_tool
async def move_note(note_id: str, new_note_id: str) -> dict[str, Any]:
    """Rename or move a note (folder notes move with all their files).

    Both ids must be the same kind: .md to .md, or folder to folder.
    Links in other notes are NOT rewritten.
    """
    return await _locked_write(
        lambda: store.move_note(note_id, new_note_id),
        lambda _: [note_id, new_note_id],
        lambda _: f"Move {note_id} to {new_note_id}",
    )


@mcp.tool()
@logged_tool
async def restore_note(note_id: str, commit: str) -> dict[str, Any]:
    """Restore a note's markdown body to how it was at a given commit.

    Get commit shas from note_history. The restore itself is a new commit,
    so nothing is ever lost — restores can be restored from.
    """

    def do_restore() -> dict[str, Any]:
        if err := store.check_note(note_id):
            return err
        if err := git_ops.restore_file(commit, store.md_relpath(note_id)):
            return err
        store.invalidate_updated_at(note_id)
        return {"status": "restored", "note_id": note_id, "restored_from": commit}

    return await _locked_write(
        do_restore,
        lambda _: [store.md_relpath(note_id)],
        lambda _: f"Restore {note_id} to {commit[:7]}",
    )


# ----------------------------------------------------------------------
# script tools


@mcp.tool()
@logged_tool
def list_scripts(note_id: str) -> dict[str, Any]:
    """List runnable scripts in a note's scripts/ folder as {name, description}.

    The description is the script's docstring or top-of-file comment.
    """
    return scripts.list_scripts(store, note_id)


@mcp.tool()
@logged_tool
async def run_script(
    note_id: str,
    script_name: str,
    args: list[str] | None = None,
    timeout_seconds: int | None = None,
    # Bare `Context` is required: FastMCP's find_context_parameter only
    # recognizes the unsubscripted class (directly or in a union).
    ctx: Context | None = None,  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Run a script from the note's scripts/ folder, cwd set to the note folder.

    Returns {exit_code, stdout, stderr, duration_seconds}. Interpreter comes
    from the shebang line or extension (.py/.sh). Default timeout 60s,
    max 300s. Progress is streamed while the script runs.
    """

    async def on_progress(message: str, elapsed: float) -> None:
        if ctx is None:
            return
        # Log notification doubles as a transport keepalive; progress
        # notifications only flow when the client sent a progressToken.
        await ctx.info(f"[{elapsed:.0f}s] {message}")
        await ctx.report_progress(progress=elapsed, message=message)

    # Hold the write lock: scripts may write into the note folder, and a
    # concurrent pull --rebase against a mutating tree would corrupt state.
    async with _write_lock:
        return await scripts.run_script(
            store,
            note_id,
            script_name,
            args,
            timeout_seconds=timeout_seconds or settings.script_timeout_default,
            max_timeout=settings.script_timeout_max,
            on_progress=on_progress,
        )


@mcp.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
async def health(request: Request) -> Response:
    """Unauthenticated liveness probe: 200 iff the working tree is readable."""
    if settings.notes_repo_path.is_dir() and os.access(settings.notes_repo_path, os.R_OK):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "repo_unreadable"}, status_code=503)


@mcp.custom_route("/auth/callback", methods=["GET"])  # type: ignore[untyped-decorator]
async def github_callback(request: Request) -> Response:
    """GitHub redirects here after login; we bounce back to the MCP client."""
    if auth_provider is None:
        return JSONResponse({"error": "auth_disabled"}, status_code=404)
    try:
        redirect_url = await auth_provider.handle_callback(
            request.query_params.get("code"), request.query_params.get("state")
        )
    except CallbackError as exc:
        return JSONResponse(
            {"error": "authorization_failed", "details": str(exc)}, status_code=exc.status
        )
    return RedirectResponse(redirect_url, status_code=302)


def main() -> None:
    setup_logging(settings.log_level)
    settings.validate_startup()
    try:
        if git_ops.enabled and not git_ops.head_valid() and settings.notes_repo_url:
            # Interrupted clone (unborn HEAD): reads would work, writes never.
            logger.warning(
                "repo has no valid HEAD (interrupted clone?); re-cloning",
                extra={"url": settings.notes_repo_url},
            )
            git_ops.clone(settings.notes_repo_url)
        if not git_ops.enabled and settings.notes_repo_url:
            logger.info("cloning notes repo", extra={"url": settings.notes_repo_url})
            git_ops.clone(settings.notes_repo_url)
        if git_ops.enabled:
            if git_ops.hard_reset_if_dirty():
                logger.warning("recovered dirty working tree at startup")
            git_ops.pull()
    except GitError as exc:
        logger.error("startup git setup failed", extra=exc.payload)
        raise SystemExit(1) from exc
    logger.info(
        "starting notes-mcp",
        extra={
            "repo_path": str(settings.notes_repo_path),
            "host": settings.host,
            "port": settings.port,
            "git_enabled": git_ops.enabled,
            "git_remote": git_ops.has_remote,
            "branch": git_ops.branch,
            "auth_enabled": settings.auth_enabled,
        },
    )
    if not settings.auth_enabled:
        logger.warning("auth disabled: PUBLIC_URL / GITHUB_OAUTH_* not set")
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
