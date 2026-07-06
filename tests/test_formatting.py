"""Tests for markdown formatting in the write flow."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest
from conftest import git

from notes_mcp.formatting import format_markdown
from notes_mcp.git_ops import GitOps, write_flow
from notes_mcp.notes import NotesStore

# A stand-in for prettier: appends a marker to each file it "formats", so
# tests don't depend on node being installed. Real-prettier behavior is
# covered by the container (prettier@3 baked into the image).
FAKE_FORMATTER = """\
#!/bin/sh
shift  # drop --write
for f in "$@"; do
  printf '\\n<!-- formatted -->\\n' >> "$f"
done
"""


@pytest.fixture
def formatter(tmp_path: Path) -> str:
    path = tmp_path / "fake-prettier"
    path.write_text(FAKE_FORMATTER)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_format_markdown_runs_on_md_only(repo: Path, formatter: str) -> None:
    (repo / "cycling-analysis" / "data.csv").write_text("a,b\n")
    ok = format_markdown(
        repo, ["bikepacking-gear/index.md", "cycling-analysis/data.csv"], formatter
    )
    assert ok
    assert "<!-- formatted -->" in (repo / "bikepacking-gear" / "index.md").read_text()
    assert "formatted" not in (repo / "cycling-analysis" / "data.csv").read_text()


def test_format_markdown_no_md_paths_is_noop(repo: Path, formatter: str) -> None:
    assert format_markdown(repo, ["cycling-analysis/ride.fit"], formatter) is False


def test_format_markdown_missing_formatter_is_soft(repo: Path) -> None:
    assert format_markdown(repo, ["bikepacking-gear/index.md"], "/nonexistent/prettier") is False
    # file untouched
    assert "formatted" not in (repo / "bikepacking-gear" / "index.md").read_text()


def test_format_markdown_failing_formatter_is_soft(repo: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad-formatter"
    bad.write_text("#!/bin/sh\nexit 1\n")
    bad.chmod(bad.stat().st_mode | stat.S_IXUSR)
    assert format_markdown(repo, ["bikepacking-gear/index.md"], str(bad)) is False


def test_write_flow_formats_before_commit(git_repo: Path, formatter: str) -> None:
    store = NotesStore(git_repo)
    ops = GitOps(repo_path=git_repo)
    result = write_flow(
        store,
        ops,
        lambda: store.append_to_note("bikepacking-gear", "- new item"),
        lambda _: ["bikepacking-gear/index.md"],
        lambda _: "Append to bikepacking-gear",
        formatter=formatter,
    )
    assert result["git"]["committed"] and result["git"]["formatted"]
    committed = subprocess.run(
        ["git", "-C", str(git_repo), "show", "HEAD:bikepacking-gear/index.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # The formatter's marker made it INTO the commit, not just the worktree.
    assert committed.rstrip().endswith("<!-- formatted -->")
    assert "- new item" in committed
    assert (
        subprocess.run(
            ["git", "-C", str(git_repo), "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        == ""
    )


def test_write_flow_commits_even_if_formatter_missing(git_repo: Path) -> None:
    store = NotesStore(git_repo)
    ops = GitOps(repo_path=git_repo)
    result = write_flow(
        store,
        ops,
        lambda: store.append_to_note("bikepacking-gear", "- unformatted item"),
        lambda _: ["bikepacking-gear/index.md"],
        lambda _: "Append to bikepacking-gear",
        formatter="/nonexistent/prettier",
    )
    assert result["git"]["committed"]
    assert result["git"]["formatted"] is False


def test_write_flow_formats_file_notes_too(git_repo: Path, formatter: str) -> None:
    (git_repo / "topic").mkdir()
    (git_repo / "topic" / "idea.md").write_text("# Idea\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-m", "add file note")
    store = NotesStore(git_repo)
    ops = GitOps(repo_path=git_repo)
    result = write_flow(
        store,
        ops,
        lambda: store.edit_note("topic/idea.md", "# Idea", "# Better Idea"),
        lambda _: [store.md_relpath("topic/idea.md")],
        lambda _: "Edit topic/idea.md",
        formatter=formatter,
    )
    assert result["git"]["formatted"]
    assert "<!-- formatted -->" in (git_repo / "topic" / "idea.md").read_text()
