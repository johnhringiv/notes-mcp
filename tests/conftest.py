"""Fixtures: temp directories mimicking the notes repo structure."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from notes_mcp.notes import NotesStore


@pytest.fixture(autouse=True, scope="session")
def _enable_info_logging() -> None:
    """INFO logging is on in production; keep it on in tests so bad
    logging calls (e.g. reserved LogRecord keys in `extra`) fail loudly."""
    logging.getLogger().setLevel(logging.INFO)


def make_note(
    repo: Path,
    note_id: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    body: str = "",
    frontmatter: bool = True,
) -> Path:
    note_dir = repo / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    if frontmatter:
        tag_str = ", ".join(tags or [])
        parts.append(
            f"---\ntitle: {title or note_id}\ntags: [{tag_str}]\ncreated: 2026-01-01\n---\n"
        )
    parts.append(body)
    (note_dir / "index.md").write_text("\n".join(parts), encoding="utf-8")
    return note_dir


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A non-git directory tree mimicking the notes repo."""
    root = tmp_path / "notes"
    root.mkdir()
    make_note(
        root,
        "bikepacking-gear",
        title="Bikepacking Gear List",
        tags=["bikepacking", "gear"],
        body="# Bikepacking Gear List\n\n## Bags\n\n- Tailfin rack\n",
    )
    make_note(
        root,
        "cycling-analysis",
        title="Cycling Analysis",
        tags=["cycling"],
        body="Analysis notes.\n",
    )
    note_dir = root / "cycling-analysis"
    (note_dir / "ride.fit").write_bytes(b"\x00\x01binary")
    scripts = note_dir / "scripts"
    scripts.mkdir()
    (scripts / "analyze.py").write_text('"""Analyze a FIT file."""\nprint("ok")\n')
    # A note without frontmatter — title should come from the first H1.
    make_note(root, "no-frontmatter", body="# Actual Title\n\nsome text\n", frontmatter=False)
    # Hidden/reserved dirs that must never appear as notes.
    (root / ".templates").mkdir()
    (root / ".templates" / "meeting.md").write_text(
        "---\ntitle: {{title}}\ntags: [{{tags}}]\ncreated: {{created}}\n---\n\n"
        "# {{title}}\n\n## Agenda\n"
    )
    (root / ".mcp").mkdir()
    return root


@pytest.fixture
def store(repo: Path) -> NotesStore:
    return NotesStore(repo)


def git(repo: Path, *args: str, extra_env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **(extra_env or {}),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


@pytest.fixture
def git_repo(repo: Path) -> Path:
    """The same tree, committed to a real git repo."""
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def git_store(git_repo: Path) -> NotesStore:
    return NotesStore(git_repo)
