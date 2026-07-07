"""add_file_to_note: write an (optionally binary) file into a note folder."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from notes_mcp.errors import error
from notes_mcp.notes import INDEX, NotesStore, is_file_note

# Sanity cap, not a security boundary — chat uploads should stay well under it.
MAX_FILE_BYTES = 20 * 1024 * 1024


def add_file_to_note(
    store: NotesStore, note_id: str, filename: str, content_b64: str
) -> dict[str, Any]:
    if err := store.check_note(note_id):
        return err
    if is_file_note(note_id):
        return error(
            "not_a_folder_note",
            note_id=note_id,
            reason="only folder notes (containing index.md) can hold extra files",
        )
    if "/" in filename or "\\" in filename or filename.startswith(".") or not filename.strip():
        return error(
            "invalid_filename",
            filename=filename,
            reason="filename must be a bare name without path separators",
        )
    if filename == INDEX:
        return error(
            "invalid_filename",
            filename=filename,
            reason="index.md is the note body; use append_to_note or edit_note",
        )
    try:
        data = base64.b64decode(content_b64, validate=True)
    except binascii.Error, ValueError:
        return error("invalid_content", reason="content_b64 is not valid base64")
    if len(data) > MAX_FILE_BYTES:
        return error("file_too_large", size=len(data), max_bytes=MAX_FILE_BYTES)

    path = store.note_dir(note_id) / filename
    overwritten = path.exists()
    path.write_bytes(data)
    store.invalidate_updated_at(note_id)
    return {
        "status": "written",
        "note_id": note_id,
        "filename": filename,
        "size": len(data),
        "overwritten": overwritten,
    }


def read_note_file(
    store: NotesStore,
    note_id: str,
    filename: str,
    start_line: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return a file's content from a folder note (utf-8 text or base64).

    `filename` is relative to the note folder and may include subdirectories
    (e.g. "scripts/analyze.py"), matching the names read_note lists.
    """
    if err := store.check_note(note_id):
        return err
    if is_file_note(note_id):
        return error(
            "not_a_folder_note",
            note_id=note_id,
            reason="file notes have no attached files; use read_note for the body",
        )
    parts = filename.replace("\\", "/").split("/")
    if not filename.strip() or any(not p or p.startswith(".") or p == ".." for p in parts):
        return error(
            "invalid_filename",
            filename=filename,
            reason="path must be relative, without dot segments",
        )
    note_dir = store.note_dir(note_id).resolve()
    path = (note_dir / filename).resolve()
    if not path.is_relative_to(note_dir):
        return error("invalid_filename", filename=filename, reason="path escapes the note folder")
    if not path.is_file():
        return error("file_not_found", note_id=note_id, filename=filename)
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return error("file_too_large", size=len(data), max_bytes=MAX_FILE_BYTES)
    from notes_mcp.notes import _file_type

    meta = {
        "note_id": note_id,
        "filename": filename,
        "size": len(data),
        "type": _file_type(filename),
    }
    try:
        if b"\x00" in data:  # NUL byte = binary, even if it decodes (git's heuristic)
            raise UnicodeDecodeError("utf-8", data, 0, 1, "binary content")
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {**meta, "encoding": "base64", "content_b64": base64.b64encode(data).decode()}
    if start_line is None:
        return {**meta, "encoding": "utf-8", "content": text}
    lines = text.splitlines()
    start = max(1, start_line)
    window = lines[start - 1 : start - 1 + max(1, min(limit, 2000))]
    return {
        **meta,
        "encoding": "utf-8",
        "content": "\n".join(window),
        "start_line": start,
        "end_line": start + len(window) - 1,
        "total_lines": len(lines),
    }
