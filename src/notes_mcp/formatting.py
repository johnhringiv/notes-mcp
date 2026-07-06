"""Markdown formatting before commits.

The notes repo's own pre-commit hook (prettier) never fires for server-side
commits — fresh clones don't set core.hooksPath and the container has no
shell trickery. Instead, the write flow formats changed markdown explicitly
before staging, so Claude's commits match the repo's prettier style.

Best-effort by design: a missing or failing formatter logs a warning and the
write proceeds unformatted. A formatting problem must never eat a note edit.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("notes_mcp.format")

_warned_missing = False


def format_markdown(repo_path: Path, rel_paths: list[str], formatter: str | None = None) -> bool:
    """Run ``prettier --write`` on repo-relative markdown files.

    Returns True if formatting ran cleanly. cwd is the repo root so any
    prettier config committed to the notes repo is respected.
    """
    global _warned_missing
    md_paths = [p for p in rel_paths if p.endswith(".md")]
    if not md_paths:
        return False
    command = formatter or shutil.which("prettier")
    if command is None:
        if not _warned_missing:
            logger.warning("prettier not found on PATH; committing unformatted markdown")
            _warned_missing = True
        return False
    try:
        proc = subprocess.run(
            [command, "--write", *md_paths],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("markdown formatting failed", extra={"reason": str(exc)})
        return False
    if proc.returncode != 0:
        logger.warning(
            "markdown formatting failed",
            extra={"files": md_paths, "stderr": proc.stderr.strip()[:500]},
        )
        return False
    return True
