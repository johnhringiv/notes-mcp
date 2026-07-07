"""Resources: imported reference documents, distinct from notes.

Resources live under a top-level ``resources/`` directory (e.g.
``resources/maeve/austral-spec.md``). They differ from notes in kind:
imported rather than authored, replaced wholesale rather than edited,
excluded from note listings, and read in ranges (a dumped book must never
be returned whole). Text-only by construction — ``add_resource`` accepts a
string, which keeps the repo greppable and small.

The future semantic layer indexes resources by default (being findable is
their whole job) and notes only when opted in via ``index: true``
frontmatter; see PRD.md.
"""

from __future__ import annotations

import re
from typing import Any

from notes_mcp.errors import error
from notes_mcp.notes import NotesStore, _first_h1, split_frontmatter

RESOURCES_DIR = "resources"
MAX_RESOURCE_BYTES = 20 * 1024 * 1024
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 2000

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_TEXT_SUFFIXES = (".md", ".txt")


def validate_resource_id(resource_id: str) -> dict[str, Any] | None:
    if not resource_id or resource_id != resource_id.strip():
        return error("invalid_resource_id", resource_id=resource_id, reason="empty or padded")
    parts = resource_id.split("/")
    if any(not _SEGMENT_RE.match(p) for p in parts):
        return error(
            "invalid_resource_id",
            resource_id=resource_id,
            reason="each path segment must start with a letter/digit; no dot segments",
        )
    if not resource_id.endswith(_TEXT_SUFFIXES):
        return error(
            "invalid_resource_id",
            resource_id=resource_id,
            reason="resources are text-first: id must end in .md or .txt "
            "(convert documents before importing)",
        )
    return None


def list_resources(store: NotesStore) -> dict[str, Any]:
    root = store.repo_path / RESOURCES_DIR
    if not root.is_dir():
        return {"resources": []}
    resources = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not path.name.endswith(_TEXT_SUFFIXES):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            fm, body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            fm, body = {}, ""
        title = fm.get("title") if isinstance(fm.get("title"), str) else None
        resources.append(
            {
                "id": rel,
                "title": title or _first_h1(body) or path.stem,
                "source": fm.get("source"),
                "fidelity": fm.get("fidelity"),
                "retrieved": str(fm.get("retrieved")) if fm.get("retrieved") else None,
                "size": path.stat().st_size,
                "updated_at": store.updated_at(f"{RESOURCES_DIR}/{rel}"),
            }
        )
    return {"resources": resources}


def add_resource(
    store: NotesStore, resource_id: str, content: str, append: bool = False
) -> dict[str, Any]:
    """Create/replace a resource, or append a chunk to one.

    Documents longer than the model can faithfully reproduce in one tool
    call arrive as a create followed by appends (see the resource-ingestion
    skill). The response reports total_lines so the sender can verify the
    transfer.
    """
    if err := validate_resource_id(resource_id):
        return err
    if not content.strip():
        return error("invalid_content", reason="content is empty")
    path = store.repo_path / RESOURCES_DIR / resource_id
    if append and not path.is_file():
        return error(
            "resource_not_found",
            resource_id=resource_id,
            reason="append requires an existing resource; create it first",
        )
    existed = path.is_file()
    if not content.endswith("\n"):
        content += "\n"
    new_size = len(content.encode()) + (path.stat().st_size if append else 0)
    if new_size > MAX_RESOURCE_BYTES:
        return error("file_too_large", size=new_size, max_bytes=MAX_RESOURCE_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    store.invalidate_updated_at(f"{RESOURCES_DIR}/{resource_id}")
    total_lines = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    return {
        "status": "appended" if append else ("updated" if existed else "added"),
        "resource_id": resource_id,
        "size": path.stat().st_size,
        "total_lines": total_lines,
    }


def read_resource(
    store: NotesStore,
    resource_id: str,
    start_line: int = 1,
    limit: int = READ_DEFAULT_LINES,
) -> dict[str, Any]:
    if err := validate_resource_id(resource_id):
        return err
    path = store.repo_path / RESOURCES_DIR / resource_id
    if not path.is_file():
        return error("resource_not_found", resource_id=resource_id)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, start_line)
    limit = max(1, min(limit, READ_MAX_LINES))
    window = lines[start - 1 : start - 1 + limit]
    return {
        "resource_id": resource_id,
        "start_line": start,
        "end_line": start + len(window) - 1,
        "total_lines": total,
        "content": "\n".join(window),
    }
