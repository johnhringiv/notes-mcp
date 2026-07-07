"""Core note operations: list, read, search, create, append, edit.

Phase 1: pure filesystem against a local clone. Git commit/push wrapping
arrives in Phase 2; the only git interaction here is *reading* per-note
timestamps (``git log -1``), because file mtimes are useless after a fresh
clone.

All public methods return plain dicts and never raise for expected failure
modes — they return ``{"error": ..., "details": ...}`` instead.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from notes_mcp.errors import error
from notes_mcp.templates import TEMPLATES_DIR, load_template, render

INDEX = "index.md"

# Note ids are folder paths relative to the repo root. Nesting is allowed
# (GitJournal can create nested folders); each segment must be a plain name.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

_TEXT_TYPES = {
    ".md": "markdown",
    ".txt": "text",
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".py": "python",
    ".sh": "shell",
    ".html": "html",
    ".xml": "xml",
}
_BINARY_TYPES = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".pdf": "pdf",
    ".fit": "fit",
    ".gpx": "gpx",
    ".zip": "archive",
    ".gz": "archive",
}


def validate_note_id(note_id: str) -> dict[str, Any] | None:
    """Return an error dict if the id is unsafe, else None."""
    if not note_id or note_id != note_id.strip():
        return error("invalid_note_id", note_id=note_id, reason="empty or surrounding whitespace")
    parts = note_id.split("/")
    for part in parts:
        if not _SEGMENT_RE.match(part):
            return error(
                "invalid_note_id",
                note_id=note_id,
                reason=(
                    "each path segment must start with a letter/digit and contain only "
                    "letters, digits, spaces, dots, hyphens, underscores"
                ),
            )
    if parts[-1] == INDEX:
        return error(
            "invalid_note_id",
            note_id=note_id,
            reason="a folder note is addressed by its folder name, without /index.md",
        )
    if any(part.endswith(".md") for part in parts[:-1]):
        return error(
            "invalid_note_id",
            note_id=note_id,
            reason="only the last path segment may end in .md",
        )
    return None


def _reserved_dir(note_id: str) -> dict[str, Any] | None:
    for reserved in (TEMPLATES_DIR, "resources"):
        if note_id == reserved or note_id.startswith(f"{reserved}/"):
            return error(
                "invalid_note_id",
                note_id=note_id,
                reason=f"{reserved}/ is reserved (use the resource tools for resources/)",
            )
    return None


def is_file_note(note_id: str) -> bool:
    """File notes are bare .md paths; folder notes contain an index.md."""
    return note_id.endswith(".md")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from a markdown document.

    Returns ``({}, text)`` when there is no valid frontmatter block.
    """
    if not text.startswith("---\n") and text != "---":
        return {}, text
    end = re.search(r"^(?:---|\.\.\.)\s*$", text[4:], flags=re.MULTILINE)
    if end is None:
        return {}, text
    raw = text[4 : 4 + end.start()]
    body = text[4 + end.end() :].lstrip("\n")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body


def _first_h1(body: str) -> str | None:
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return None


def _file_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return _TEXT_TYPES.get(suffix) or _BINARY_TYPES.get(suffix, "binary" if suffix else "text")


class NotesStore:
    """All note operations against one repo working tree."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve()
        self._updated_at_cache: dict[str, str] = {}
        self._is_git_repo: bool | None = None

    # ------------------------------------------------------------------
    # helpers

    def note_dir(self, note_id: str) -> Path:
        return self.repo_path / note_id

    def md_relpath(self, note_id: str) -> str:
        """Repo-relative path of the note's markdown body (both note kinds)."""
        return note_id if is_file_note(note_id) else f"{note_id}/{INDEX}"

    def md_path(self, note_id: str) -> Path:
        return self.repo_path / self.md_relpath(note_id)

    def _note_exists(self, note_id: str) -> bool:
        return self.md_path(note_id).is_file()

    def check_note(self, note_id: str) -> dict[str, Any] | None:
        """Validate the id and existence; return an error dict or None."""
        if err := validate_note_id(note_id):
            return err
        if not self._note_exists(note_id):
            return error("note_not_found", note_id=note_id)
        return None

    def is_git_repo(self) -> bool:
        if self._is_git_repo is None:
            self._is_git_repo = (self.repo_path / ".git").exists()
        return self._is_git_repo

    def invalidate_updated_at(self, note_id: str) -> None:
        self._updated_at_cache.pop(note_id, None)

    def refresh_all_updated_at(self) -> None:
        """Drop the whole timestamp cache (call after every git pull)."""
        self._updated_at_cache.clear()

    def updated_at(self, note_id: str) -> str:
        cached = self._updated_at_cache.get(note_id)
        if cached is not None:
            return cached
        value = self._updated_at_from_git(note_id) or self._updated_at_from_mtime(note_id)
        self._updated_at_cache[note_id] = value
        return value

    def _updated_at_from_git(self, note_id: str) -> str | None:
        if not self.is_git_repo():
            return None
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "log", "-1", "--format=%cI", "--", note_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stamp = result.stdout.strip()
        return stamp if result.returncode == 0 and stamp else None

    def _updated_at_from_mtime(self, note_id: str) -> str:
        target = self.md_path(note_id) if is_file_note(note_id) else self.note_dir(note_id)
        if target.is_dir():
            mtimes = [p.stat().st_mtime for p in target.rglob("*") if p.is_file()]
            latest = max(mtimes, default=target.stat().st_mtime)
        else:
            latest = target.stat().st_mtime
        return dt.datetime.fromtimestamp(latest, tz=dt.UTC).isoformat(timespec="seconds")

    def _iter_note_ids(self) -> list[str]:
        found: list[str] = []

        def walk(directory: Path) -> None:
            for child in sorted(directory.iterdir()):
                if child.name.startswith("."):
                    continue
                if directory == self.repo_path and child.name == "resources":
                    continue  # resources are documents, not notes (see resources.py)
                if child.is_dir():
                    if child.name == "scripts":
                        continue
                    if (child / INDEX).is_file():
                        found.append(child.relative_to(self.repo_path).as_posix())
                    walk(child)
                elif child.suffix.lower() == ".md" and child.name != INDEX:
                    found.append(child.relative_to(self.repo_path).as_posix())

        walk(self.repo_path)
        return found

    def _read_meta(self, note_id: str) -> tuple[dict[str, Any], str]:
        text = self.md_path(note_id).read_text(encoding="utf-8")
        return split_frontmatter(text)

    @staticmethod
    def _title_and_tags(note_id: str, fm: dict[str, Any], body: str) -> tuple[str, list[str]]:
        fallback = note_id.rsplit("/", 1)[-1].removesuffix(".md")
        title = fm.get("title")
        if not isinstance(title, str) or not title.strip():
            title = _first_h1(body) or fallback
        raw_tags = fm.get("tags")
        if isinstance(raw_tags, str):
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        elif isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        else:
            tags = []
        return title.strip(), tags

    # ------------------------------------------------------------------
    # tools

    def list_notes(self, filter: str | None = None) -> dict[str, Any]:
        notes: list[dict[str, Any]] = []
        for note_id in self._iter_note_ids():
            try:
                fm, body = self._read_meta(note_id)
            except OSError, UnicodeDecodeError:
                fm, body = {}, ""
            title, tags = self._title_and_tags(note_id, fm, body)
            if filter:
                needle = filter.lower()
                haystack = " ".join([note_id, title, *tags]).lower()
                if needle not in haystack:
                    continue
            notes.append(
                {
                    "id": note_id,
                    "title": title,
                    "tags": tags,
                    "indexed": fm.get("index") is True,
                    "updated_at": self.updated_at(note_id),
                    "path": self.md_relpath(note_id),
                }
            )
        notes.sort(key=lambda n: n["updated_at"], reverse=True)
        return {"notes": notes}

    def read_note(self, note_id: str) -> dict[str, Any]:
        if err := self.check_note(note_id):
            return err
        content = self.md_path(note_id).read_text(encoding="utf-8")
        fm, _ = split_frontmatter(content)
        if is_file_note(note_id):
            return {"content": content, "files": [], "frontmatter": fm}
        note_dir = self.note_dir(note_id)
        files = []
        for path in sorted(note_dir.rglob("*")):
            rel = path.relative_to(note_dir).as_posix()
            if not path.is_file() or rel == INDEX or path.name.startswith(".git"):
                continue
            # A nested folder that is itself a note belongs to that note.
            if any(
                (note_dir / parent / INDEX).is_file()
                for parent in Path(rel).parents
                if str(parent) != "."
            ):
                continue
            files.append({"name": rel, "size": path.stat().st_size, "type": _file_type(rel)})
        return {"content": content, "files": files, "frontmatter": fm}

    def search_notes(self, query: str, max_results: int = 20) -> dict[str, Any]:
        if not query.strip():
            return error("invalid_query", reason="query is empty")
        max_results = max(1, min(max_results, 100))
        try:
            proc = subprocess.run(
                # --fixed-strings: queries come from chat, treat them as
                # literal text, not regex (a stray "(" must not error out).
                ["rg", "--json", "--smart-case", "--fixed-strings", "--", query, "."],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            return error("search_failed", reason="ripgrep (rg) is not installed")
        except subprocess.TimeoutExpired:
            return error("search_failed", reason="search timed out after 15s")
        if proc.returncode not in (0, 1):  # 1 = no matches
            return error("search_failed", reason=proc.stderr.strip()[:500])

        results: list[dict[str, Any]] = []
        for raw_line in proc.stdout.splitlines():
            if len(results) >= max_results:
                break
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            rel_file = Path(data["path"]["text"]).as_posix().removeprefix("./")
            line_number = data["line_number"]
            hit: dict[str, Any] = {
                "file": rel_file,
                "line_number": line_number,
                "snippet": self._snippet(rel_file, line_number),
            }
            if rel_file.startswith("resources/"):
                hit["resource_id"] = rel_file.removeprefix("resources/")
                hit["note_id"] = None
            else:
                hit["note_id"] = self._match_note_id(rel_file)
            results.append(hit)
        return {"results": results}

    def _owning_note(self, rel_file: str) -> str | None:
        parent = Path(rel_file).parent
        while str(parent) != ".":
            if (self.repo_path / parent / INDEX).is_file():
                return parent.as_posix()
            parent = parent.parent
        return None

    def _match_note_id(self, rel_file: str) -> str | None:
        """The note a search hit belongs to: the file itself if it is a file
        note, else the nearest ancestor folder note."""
        path = Path(rel_file)
        if path.name == INDEX:
            owner = path.parent.as_posix()
            return owner if owner != "." else None
        if path.suffix.lower() == ".md" and not any(part.startswith(".") for part in path.parts):
            return rel_file
        return self._owning_note(rel_file)

    def _snippet(self, rel_file: str, line_number: int, context: int = 2) -> str:
        try:
            lines = (
                (self.repo_path / rel_file)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            return ""
        lo = max(0, line_number - 1 - context)
        hi = min(len(lines), line_number + context)
        return "\n".join(lines[lo:hi])

    def create_note(
        self,
        note_id: str,
        title: str,
        tags: list[str] | None = None,
        template: str | None = None,
    ) -> dict[str, Any]:
        if err := validate_note_id(note_id):
            return err
        if not title.strip():
            return error("invalid_title", reason="title is empty")
        if err := _reserved_dir(note_id):
            return err
        md_path = self.md_path(note_id)
        if (self.note_dir(note_id) if not is_file_note(note_id) else md_path).exists():
            return error("note_already_exists", note_id=note_id)
        template_text = load_template(self.repo_path, template)
        if isinstance(template_text, dict):
            return template_text
        created = dt.date.today().isoformat()
        content = render(
            template_text, title=title.strip(), tags=tags or [], created=created, note_id=note_id
        )
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content, encoding="utf-8")
        self.invalidate_updated_at(note_id)
        return {"status": "created", "note_id": note_id, "path": self.md_relpath(note_id)}

    def append_to_note(
        self, note_id: str, content: str, section: str | None = None
    ) -> dict[str, Any]:
        if err := self.check_note(note_id):
            return err
        if not content:
            return error("invalid_content", reason="content is empty")
        index_path = self.md_path(note_id)
        text = index_path.read_text(encoding="utf-8")
        block = content.strip("\n")

        if section is None:
            new_text = text.rstrip("\n") + "\n\n" + block + "\n" if text.strip() else block + "\n"
            appended_to = "end"
        else:
            new_text, found = _append_under_section(text, section, block)
            appended_to = f"section: {section}" + ("" if found else " (created)")

        index_path.write_text(new_text, encoding="utf-8")
        self.invalidate_updated_at(note_id)
        return {"status": "appended", "note_id": note_id, "appended_to": appended_to}

    def write_note(self, note_id: str, content: str) -> dict[str, Any]:
        if err := self.check_note(note_id):
            return err
        if not content.strip():
            return error(
                "invalid_content",
                reason="content is empty; deleting notes via MCP is not supported",
            )
        if not content.endswith("\n"):
            content += "\n"
        self.md_path(note_id).write_text(content, encoding="utf-8")
        self.invalidate_updated_at(note_id)
        return {"status": "written", "note_id": note_id, "bytes": len(content.encode())}

    def move_note(self, note_id: str, new_note_id: str) -> dict[str, Any]:
        if err := self.check_note(note_id):
            return err
        if err := validate_note_id(new_note_id):
            return err
        if is_file_note(note_id) != is_file_note(new_note_id):
            return error(
                "invalid_note_id",
                note_id=new_note_id,
                reason="source and destination must be the same kind "
                "(both .md files or both folders)",
            )
        if err := _reserved_dir(new_note_id):
            return err
        source = self.md_path(note_id) if is_file_note(note_id) else self.note_dir(note_id)
        dest = (
            self.md_path(new_note_id) if is_file_note(new_note_id) else self.note_dir(new_note_id)
        )
        if dest.exists():
            return error("note_already_exists", note_id=new_note_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, dest)
        self.invalidate_updated_at(note_id)
        self.invalidate_updated_at(new_note_id)
        return {"status": "moved", "from": note_id, "to": new_note_id}

    def edit_note(self, note_id: str, old_str: str, new_str: str) -> dict[str, Any]:
        if err := self.check_note(note_id):
            return err
        if not old_str:
            return error("invalid_edit", reason="old_str is empty")
        if old_str == new_str:
            return error("invalid_edit", reason="old_str and new_str are identical")
        index_path = self.md_path(note_id)
        text = index_path.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            return error("no_match", note_id=note_id, old_str=old_str[:200])
        if count > 1:
            return error(
                "multiple_matches",
                note_id=note_id,
                count=count,
                hint="include more surrounding context in old_str to make it unique",
            )
        index_path.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        self.invalidate_updated_at(note_id)
        return {"status": "edited", "note_id": note_id}


def _heading_at(line: str, in_fence: bool) -> tuple[int, str] | None:
    if in_fence:
        return None
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _append_under_section(text: str, section: str, block: str) -> tuple[str, bool]:
    """Insert block at the end of the named section; create the section if missing.

    Returns (new_text, section_existed). Heading match is case-insensitive on
    the heading text at any level; fenced code blocks are ignored.
    """
    lines = text.splitlines()
    target = section.strip().lower()
    start = level = None
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        heading = _heading_at(line, in_fence)
        if heading and heading[1].lower() == target:
            start, level = i, heading[0]
            break

    if start is None:
        base = text.rstrip("\n")
        prefix = base + "\n\n" if base.strip() else ""
        return f"{prefix}## {section.strip()}\n\n{block}\n", False

    assert level is not None
    end = len(lines)
    in_fence = False
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        heading = _heading_at(lines[i], in_fence)
        if heading and heading[0] <= level:
            end = i
            break

    section_body = lines[start + 1 : end]
    while section_body and not section_body[-1].strip():
        section_body.pop()
    new_section = [lines[start], *section_body, "", *block.splitlines()]
    rest = lines[end:]
    new_lines = lines[:start] + new_section + ([""] if rest else []) + rest
    return "\n".join(new_lines).rstrip("\n") + "\n", True
