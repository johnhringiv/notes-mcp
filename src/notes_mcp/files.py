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
