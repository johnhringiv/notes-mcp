"""Template rendering for create_note.

Templates live in ``<repo>/.templates/``. A built-in default is compiled in
so create_note works even before the notes repo has a ``.templates/``
directory — no repo bootstrap step is required.

Placeholder syntax: ``{{title}}``, ``{{tags}}``, ``{{created}}``,
``{{note_id}}``. ``{{tags}}`` renders as a YAML inline list body, e.g.
``tag1, tag2`` (templates put the brackets around it: ``tags: [{{tags}}]``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from notes_mcp.errors import error

DEFAULT_TEMPLATE = """\
---
title: {{title}}
tags: [{{tags}}]
created: {{created}}
---

# {{title}}
"""

TEMPLATES_DIR = ".templates"


def render(template_text: str, *, title: str, tags: list[str], created: str, note_id: str) -> str:
    values = {
        "title": title,
        "tags": ", ".join(tags),
        "created": created,
        "note_id": note_id,
    }
    out = template_text
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def load_template(repo_path: Path, name: str | None) -> str | dict[str, Any]:
    """Return template text, or a structured error dict for a bad name."""
    if name is None:
        return DEFAULT_TEMPLATE
    if "/" in name or "\\" in name or name.startswith("."):
        return error("invalid_template", name=name, reason="template name must be a bare filename")
    candidates = [name] if "." in name else [name, f"{name}.md"]
    for candidate in candidates:
        path = repo_path / TEMPLATES_DIR / candidate
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return error(
        "template_not_found",
        name=name,
        looked_in=f"{TEMPLATES_DIR}/",
        hint="omit template to use the built-in default",
    )
