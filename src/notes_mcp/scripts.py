"""list_scripts and run_script: subprocess execution inside a note folder.

run_script is async and streams periodic progress events via an injected
callback (server.py wires it to MCP progress/log notifications) so long
executions keep the HTTP connection alive past Cloudflare's ~100s idle
timeout. Limits enforced here: wall-clock timeout, stdout cap (1 MB),
stderr cap (256 KB) — output beyond a cap is discarded, not fatal.
"""

from __future__ import annotations

import ast
import asyncio
import os
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from notes_mcp.errors import error
from notes_mcp.logging import logger
from notes_mcp.notes import NotesStore, is_file_note

SCRIPTS_DIR = "scripts"
STDOUT_CAP = 1024 * 1024
STDERR_CAP = 256 * 1024
KEEPALIVE_SECONDS: int = 15  # tests shrink this; annotate so it isn't Literal[15]
DESCRIPTION_CHARS = 200

ProgressFn = Callable[[str, float], Awaitable[None]]

_EXTENSION_INTERPRETERS = {".py": ["python3"], ".sh": ["bash"]}


def _script_description(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if path.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
            if doc:
                return doc.strip()[:DESCRIPTION_CHARS]
        except SyntaxError:
            pass
    comment_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            comment_lines.append(stripped.lstrip("#").strip())
        elif comment_lines or stripped:
            break
    return " ".join(comment_lines)[:DESCRIPTION_CHARS]


def _resolve_command(path: Path) -> list[str] | None:
    """Interpreter from shebang or extension; None if unrecognized."""
    try:
        first_line = path.open(encoding="utf-8", errors="replace").readline().strip()
    except OSError:
        return None
    if first_line.startswith("#!"):
        tokens = first_line[2:].split()
        if tokens:
            return [*tokens, str(path)]
    interp = _EXTENSION_INTERPRETERS.get(path.suffix.lower())
    if interp:
        return [*interp, str(path)]
    return None


def list_scripts(store: NotesStore, note_id: str) -> dict[str, Any]:
    if err := store.check_note(note_id):
        return err
    if is_file_note(note_id):
        return {"scripts": []}  # file notes have no scripts/ folder
    scripts_dir = store.note_dir(note_id) / SCRIPTS_DIR
    if not scripts_dir.is_dir():
        return {"scripts": []}
    scripts = [
        {"name": path.name, "description": _script_description(path)}
        for path in sorted(scripts_dir.iterdir())
        if path.is_file() and not path.name.startswith(".")
    ]
    return {"scripts": scripts}


async def _drain(
    stream: asyncio.StreamReader, cap: int, on_chunk: Callable[[bytes], None]
) -> tuple[bytes, bool]:
    """Read a pipe to EOF, keeping at most `cap` bytes; report each chunk."""
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(kept), truncated
        on_chunk(chunk)
        room = cap - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True


async def run_script(
    store: NotesStore,
    note_id: str,
    script_name: str,
    args: list[str] | None = None,
    timeout_seconds: int = 60,
    max_timeout: int = 300,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    if err := store.check_note(note_id):
        return err
    if is_file_note(note_id):
        return error(
            "not_a_folder_note",
            note_id=note_id,
            reason="only folder notes (containing index.md) can have scripts",
        )
    if "/" in script_name or "\\" in script_name or script_name.startswith("."):
        return error("invalid_script_name", script_name=script_name)
    script_path = store.note_dir(note_id) / SCRIPTS_DIR / script_name
    if not script_path.is_file():
        return error("script_not_found", note_id=note_id, script_name=script_name)
    command = _resolve_command(script_path)
    if command is None:
        return error(
            "unrecognized_interpreter",
            script_name=script_name,
            reason="script needs a shebang line or a .py/.sh extension",
        )
    timeout = max(1, min(timeout_seconds, max_timeout))
    note_dir = store.note_dir(note_id)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *command,
        *(args or []),
        cwd=note_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # so we can kill the whole process group
    )
    assert proc.stdout is not None and proc.stderr is not None

    latest_line = ""

    def note_chunk(chunk: bytes) -> None:
        nonlocal latest_line
        tail = chunk.decode("utf-8", errors="replace").strip().splitlines()
        if tail:
            latest_line = tail[-1][:200]

    stdout_task = asyncio.create_task(_drain(proc.stdout, STDOUT_CAP, note_chunk))
    stderr_task = asyncio.create_task(_drain(proc.stderr, STDERR_CAP, lambda _: None))

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            if on_progress is not None:
                elapsed = time.monotonic() - start
                await on_progress(latest_line or "running", elapsed)

    keepalive_task = asyncio.create_task(keepalive())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            proc.kill()
        await proc.wait()
    finally:
        keepalive_task.cancel()

    stdout_bytes, stdout_truncated = await stdout_task
    stderr_bytes, stderr_truncated = await stderr_task
    duration = round(time.monotonic() - start, 2)

    log_extra = {
        "note_id": note_id,
        "script": script_name,
        "script_args": args or [],  # "args" is reserved on LogRecord
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout_bytes),
        "stderr_bytes": len(stderr_bytes),
    }
    logger.info("script execution", extra=log_extra)

    if timed_out:
        return error(
            "timeout",
            timeout_seconds=timeout,
            duration_seconds=duration,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )
    return {
        "exit_code": proc.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "duration_seconds": duration,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
