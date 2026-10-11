"""FastMCP app and tool registration.

Read tools call the store directly. Write tools run through `_write_flow`:
pull --rebase → mutate → add/commit/push, serialized by one process-wide
asyncio lock so concurrent tool calls can't interleave git operations
(see PRD.md, git workflow). The blocking git/filesystem work runs in a worker
thread to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from functools import wraps
from importlib.metadata import version as _package_version
from typing import Any

import anyio.to_thread
import uvicorn
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from notes_mcp import admin, files, resources, scripts
from notes_mcp.access import NONE
from notes_mcp.auth import SCOPES, GitHubUpstream, NotesOAuthProvider, callback_url
from notes_mcp.config import Settings
from notes_mcp.errors import error
from notes_mcp.git_ops import GitError, GitOps, write_flow
from notes_mcp.logging import logged_tool, logger, setup_logging
from notes_mcp.members import LOCAL, MemberStore, Principal
from notes_mcp.notes import NotesStore

VERSION = _package_version("notes-mcp")

settings = Settings.from_env()
members = MemberStore(settings.oauth_state_dir / "members.json", settings.admin_users)
store = NotesStore(settings.notes_repo_path)
git_ops = GitOps(
    repo_path=settings.notes_repo_path,
    branch=settings.notes_repo_branch,
    author_name=settings.git_author_name,
    author_email=settings.git_author_email,
    token=settings.github_token,
)

_write_lock = asyncio.Lock()


def _build_auth() -> tuple[NotesOAuthProvider | None, AuthSettings | None]:
    if not settings.auth_enabled:
        return None, None
    assert settings.public_url
    assert settings.github_oauth_client_id and settings.github_oauth_client_secret
    provider = NotesOAuthProvider(
        issuer_url=settings.public_url,
        upstream=GitHubUpstream(
            settings.github_oauth_client_id,
            settings.github_oauth_client_secret,
            callback_url(settings.public_url),
        ),
        members=members,
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


def _current_subject() -> str | None:
    """The authenticated user of the current tool call (None with auth off).

    Read from the HTTP request riding on the per-message request context,
    not the SDK's auth contextvar: a streamable HTTP session task inherits
    contextvars from the request that created it, so the contextvar can
    report a stale user.
    """
    try:
        request = mcp.get_context().request_context.request
    except ValueError:  # no request in flight (e.g. direct calls in tests)
        return None
    user = request.scope.get("user") if isinstance(request, Request) else None
    return user.access_token.subject if isinstance(user, AuthenticatedUser) else None


# A request with no resolvable principal while auth is on: sees nothing.
_DENIED = Principal(subject="", name="", email="", grants=NONE, is_admin=False)


def _principal() -> Principal:
    """Who is calling, with their current grants (re-read on every call)."""
    if not settings.auth_enabled:
        return LOCAL
    subject = _current_subject()
    if subject is None:
        return _DENIED
    return members.resolve(subject, members.generation(subject)) or _DENIED


def _commit_author() -> tuple[str, str] | None:
    """Git author for the current user; the server stays the committer."""
    if not settings.auth_enabled:
        return None
    principal = _principal()
    return (principal.name, principal.email) if principal.subject else None


# ----------------------------------------------------------------------
# access control (see access.py)
#
# Every tool is wrapped by `guarded`, which declares which arguments name
# paths and how they are used; the wrapper checks them before the tool runs.
# Tools that return many paths (list/search) declare filters_output and
# filter with _principal().grants themselves. Registration fails at import if any tool
# lacks a declaration, so a new tool can't silently skip the check.

_GUARDED: set[str] = set()
RESOURCE_PREFIX = f"{resources.RESOURCES_DIR}/"


def _permits(can: Callable[[str], bool], rel: str) -> bool:
    """`can` for the path and for where it really points.

    A symlink committed into a shared folder (kitchen/x.md → ../journal/...)
    must not carry the share with it.
    """
    if not can(rel):
        return False
    real = (store.repo_path / rel).resolve()
    if not real.is_relative_to(store.repo_path):
        return False
    real_rel = real.relative_to(store.repo_path).as_posix()
    return real_rel == rel or can(real_rel)


def guarded(
    *,
    read: tuple[str, ...] = (),
    write: tuple[str, ...] = (),
    resources_read: tuple[str, ...] = (),
    resources_write: tuple[str, ...] = (),
    admin_only: bool = False,
    filters_output: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a tool's access needs by argument name.

    Unreadable paths answer exactly like missing ones (no existence leak).
    Unwritable paths get `forbidden`, the same whether or not they exist.
    """
    declared = read or write or resources_read or resources_write or admin_only
    assert declared or filters_output, "guarded() needs at least one declaration"

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        tool_name = getattr(fn, "__name__", "tool")

        def check(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
            principal = _principal()
            grants = principal.grants
            if admin_only and not principal.is_admin:
                return error("forbidden", reason=f"{tool_name} is limited to admins")
            bound = sig.bind_partial(*args, **kwargs).arguments
            for names, prefix, can, deny in (
                (read, "", grants.can_read, lambda v: error("note_not_found", note_id=v)),
                (
                    resources_read,
                    RESOURCE_PREFIX,
                    grants.can_read,
                    lambda v: error("resource_not_found", resource_id=v),
                ),
                (
                    write,
                    "",
                    grants.can_write,
                    lambda v: error("forbidden", note_id=v, reason="no write access to this path"),
                ),
                (
                    resources_write,
                    RESOURCE_PREFIX,
                    grants.can_write,
                    lambda v: error(
                        "forbidden", resource_id=v, reason="no write access to this path"
                    ),
                ),
            ):
                for name in names:
                    value = bound.get(name)
                    if not isinstance(value, str) or not _permits(can, prefix + value):
                        return deny(value)
            return None

        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if denied := check(args, kwargs):
                    return denied
                return await fn(*args, **kwargs)

            wrapper: Callable[..., Any] = async_wrapper
        else:

            @wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                if denied := check(args, kwargs):
                    return denied
                return fn(*args, **kwargs)

            wrapper = sync_wrapper
        _GUARDED.add(tool_name)
        return wrapper

    return decorate


def _assert_all_tools_guarded() -> None:
    unguarded = {t.name for t in mcp._tool_manager.list_tools()} - _GUARDED
    if unguarded:
        raise RuntimeError(f"tools registered without an access declaration: {sorted(unguarded)}")


async def _locked_write(
    mutate: Callable[[], dict[str, Any]],
    commit_paths: Callable[[dict[str, Any]], list[str]],
    message: Callable[[dict[str, Any]], str],
    format_markdown_files: bool = True,
) -> dict[str, Any]:
    author = _commit_author()
    async with _write_lock:
        return await anyio.to_thread.run_sync(
            write_flow,
            store,
            git_ops,
            mutate,
            commit_paths,
            message,
            None,
            format_markdown_files,
            author,
        )


# ----------------------------------------------------------------------
# read tools


@mcp.tool()
@logged_tool
@guarded(filters_output=True)
def list_notes(filter: str | None = None) -> dict[str, Any]:
    """List all notes as {id, title, tags, updated_at, path}, newest first.

    A note is either a bare markdown file (id like "ncc/parser_flow.md") or
    a folder containing index.md plus data files/scripts (id like
    "cycling-analysis"). `filter` is an optional substring matched
    (case-insensitively) against each note's id, title, and tags.
    """
    grants = _principal().grants
    listing = store.list_notes(filter)
    listing["notes"] = [n for n in listing["notes"] if _permits(grants.can_read, n["id"])]
    return listing


@mcp.tool()
@logged_tool
@guarded(read=("note_id",))
def read_note(note_id: str) -> dict[str, Any]:
    """Read a note: its index.md content, frontmatter, and sibling files.

    `note_id` is the note's folder name relative to the repo root, as
    returned by list_notes.
    """
    return store.read_note(note_id)


@mcp.tool()
@logged_tool
@guarded(filters_output=True)
def search_notes(query: str, max_results: int = 20) -> dict[str, Any]:
    """Full-text search (ripgrep, literal, smart-case) across all notes.

    Returns matches as {note_id, file, line_number, snippet} with ±2 lines
    of context around each match. Binary files are skipped.
    """
    grants = _principal().grants
    return store.search_notes(query, max_results, allow=lambda rel: _permits(grants.can_read, rel))


# ----------------------------------------------------------------------
# write tools (git-wrapped)


@mcp.tool()
@logged_tool
@guarded(write=("note_id",))
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
@guarded(write=("note_id",))
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
@guarded(write=("note_id",))
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
@guarded(write=("note_id",))
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
@guarded(read=("note_id",))
def note_history(note_id: str, limit: int = 10) -> dict[str, Any]:
    """List the git commits that touched a note, newest first.

    Returns {history: [{commit, author, date, message}]}. Use a commit sha
    with restore_note to bring back an older version.
    """
    if err := store.check_note(note_id):
        return err
    path = note_id if not note_id.endswith(".md") else store.md_relpath(note_id)
    grants = _principal().grants
    history = []
    for entry in git_ops.file_history(path, limit):
        touched = entry.pop("files")
        if not all(grants.can_read(f) for f in touched):
            # Commit messages name paths (e.g. "Move a to b"); don't show
            # one that also touched something this user can't see.
            entry["message"] = "(message hidden: commit also changed paths you can't access)"
        history.append(entry)
    return {"history": history}


@mcp.tool()
@logged_tool
@guarded(read=("note_id",))
def read_note_file(
    note_id: str, filename: str, start_line: int | None = None, limit: int = 500
) -> dict[str, Any]:
    """Read an attached file from a folder note.

    `filename` is relative to the note folder as listed by read_note (e.g.
    "weights.csv" or "scripts/analyze.py"). Text comes back as utf-8
    `content`; binary as base64 `content_b64` (check `encoding`). For large
    text files pass start_line to read a window (limit lines, max 2000)
    instead of the whole file.
    """
    return files.read_note_file(store, note_id, filename, start_line, limit)


@mcp.tool()
@logged_tool
@guarded(write=("note_id",))
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
@guarded(write=("note_id", "new_note_id"))
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
@guarded(write=("note_id",))
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
# resource tools (imported reference documents; see resources.py and PRD.md)


@mcp.tool()
@logged_tool
@guarded(filters_output=True)
def list_resources() -> dict[str, Any]:
    """List imported reference documents (articles, specs, papers).

    Resources are reference material, distinct from notes: read them with
    read_resource (ranged), never edit them in place. Returns
    {resources: [{id, title, source, fidelity, retrieved, size, updated_at}]}.
    """
    grants = _principal().grants
    listing = resources.list_resources(store)
    listing["resources"] = [
        r for r in listing["resources"] if _permits(grants.can_read, RESOURCE_PREFIX + r["id"])
    ]
    return listing


@mcp.tool()
@logged_tool
@guarded(resources_read=("resource_id",))
def read_resource(resource_id: str, start_line: int = 1, limit: int = 200) -> dict[str, Any]:
    """Read a window of a resource document (never the whole thing).

    Returns {content, start_line, end_line, total_lines}; page by advancing
    start_line. Use search_notes first to find the relevant line numbers —
    search covers resources and reports hits with a resource_id.
    """
    return resources.read_resource(store, resource_id, start_line, limit)


@mcp.tool()
@logged_tool
@guarded(resources_write=("resource_id",))
async def add_resource(resource_id: str, content: str, append: bool = False) -> dict[str, Any]:
    """Import a reference document, or append the next chunk of one.

    `resource_id` is a path like "maeve/austral-spec.md" (.md or .txt only —
    convert documents to text first). Long documents must be transferred in
    chunks: first call creates, subsequent calls use append=true; verify
    total_lines afterwards. Start the document with frontmatter recording
    source (URL), retrieved (date), and fidelity ("verbatim" or "digest").
    Without append, an existing resource is replaced wholesale.
    """
    return await _locked_write(
        lambda: resources.add_resource(store, resource_id, content, append),
        lambda _: [f"resources/{resource_id}"],
        lambda r: (
            f"Add resource: {resource_id}"
            if r["status"] == "added"
            else f"Update resource: {resource_id}"
        ),
        format_markdown_files=False,  # imported documents stay verbatim
    )


# ----------------------------------------------------------------------
# script tools


@mcp.tool()
@logged_tool
@guarded(read=("note_id",))
def list_scripts(note_id: str) -> dict[str, Any]:
    """List runnable scripts in a note's scripts/ folder as {name, description}.

    The description is the script's docstring or top-of-file comment.
    """
    return scripts.list_scripts(store, note_id)


@mcp.tool()
@logged_tool
@guarded(admin_only=True)
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


_assert_all_tools_guarded()


@mcp.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
async def health(request: Request) -> Response:
    """Unauthenticated liveness probe: 200 iff the working tree is readable."""
    if settings.notes_repo_path.is_dir() and os.access(settings.notes_repo_path, os.R_OK):
        return JSONResponse({"status": "ok", "version": VERSION})
    return JSONResponse({"status": "repo_unreadable", "version": VERSION}, status_code=503)


def _grantable_paths() -> list[str]:
    resource_ids = [r["id"] for r in resources.list_resources(store)["resources"]]
    return admin.grantable_paths(store.list_note_ids(), resource_ids)


admin.register(mcp, auth_provider, members, _grantable_paths)


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
    if settings.auth_enabled and not git_ops.enabled:
        # Without git every write is permanent and unattributed; members are
        # promised neither. Local dev (no PUBLIC_URL) may run without it.
        logger.error("auth is enabled but the repo is not a git repository; refusing to start")
        raise SystemExit(1)
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
            **(members.summary() if settings.auth_enabled else {}),
        },
    )
    if not settings.auth_enabled:
        logger.warning("auth disabled: PUBLIC_URL not set")
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
