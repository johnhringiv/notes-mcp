"""Tests for v0.2 tools: write_note, move_note, read_note_file, history, restore."""

from __future__ import annotations

import base64
from pathlib import Path

from conftest import git, make_note

from notes_mcp.files import read_note_file
from notes_mcp.git_ops import GitOps, write_flow
from notes_mcp.notes import NotesStore

# ----------------------------------------------------------------------
# write_note


def test_write_note_replaces_body(store: NotesStore, repo: Path) -> None:
    result = store.write_note("cycling-analysis", "# Rewritten\n\nAll new.")
    assert result["status"] == "written"
    text = (repo / "cycling-analysis" / "index.md").read_text()
    assert text == "# Rewritten\n\nAll new.\n"  # newline ensured


def test_write_note_rejects_empty_and_missing(store: NotesStore) -> None:
    assert store.write_note("cycling-analysis", "   ")["error"] == "invalid_content"
    assert store.write_note("nope", "x")["error"] == "note_not_found"


def test_write_note_on_file_note(store: NotesStore, repo: Path) -> None:
    (repo / "topic").mkdir()
    (repo / "topic" / "idea.md").write_text("old\n")
    assert store.write_note("topic/idea.md", "new body")["status"] == "written"
    assert (repo / "topic" / "idea.md").read_text() == "new body\n"


# ----------------------------------------------------------------------
# move_note


def test_move_file_note(store: NotesStore, repo: Path) -> None:
    (repo / "topic").mkdir()
    (repo / "topic" / "idea.md").write_text("body\n")
    result = store.move_note("topic/idea.md", "archive/idea.md")
    assert result == {"status": "moved", "from": "topic/idea.md", "to": "archive/idea.md"}
    assert (repo / "archive" / "idea.md").read_text() == "body\n"
    assert not (repo / "topic" / "idea.md").exists()


def test_move_folder_note_with_files(store: NotesStore, repo: Path) -> None:
    result = store.move_note("cycling-analysis", "sports/cycling")
    assert result["status"] == "moved"
    assert (repo / "sports" / "cycling" / "index.md").is_file()
    assert (repo / "sports" / "cycling" / "ride.fit").is_file()
    assert (repo / "sports" / "cycling" / "scripts" / "analyze.py").is_file()
    assert not (repo / "cycling-analysis").exists()


def test_move_rejects_kind_mismatch_and_collisions(store: NotesStore, repo: Path) -> None:
    assert store.move_note("cycling-analysis", "cycling.md")["error"] == "invalid_note_id"
    assert store.move_note("cycling-analysis", "bikepacking-gear")["error"] == "note_already_exists"
    assert store.move_note("nope", "other")["error"] == "note_not_found"
    assert store.move_note("cycling-analysis", ".templates/x")["error"] == "invalid_note_id"


# ----------------------------------------------------------------------
# read_note_file


def test_read_note_file_text_and_binary(store: NotesStore) -> None:
    result = read_note_file(store, "cycling-analysis", "scripts/analyze.py")
    assert result["encoding"] == "utf-8"
    assert "Analyze a FIT file" in result["content"]
    result = read_note_file(store, "cycling-analysis", "ride.fit")
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content_b64"]).startswith(b"\x00\x01")


def test_read_note_file_rejects_escapes(store: NotesStore, repo: Path) -> None:
    for bad in ["../bikepacking-gear/index.md", "/etc/passwd", ".git/config", "a/../../x"]:
        assert read_note_file(store, "cycling-analysis", bad)["error"] == "invalid_filename", bad
    assert read_note_file(store, "cycling-analysis", "ghost.csv")["error"] == "file_not_found"
    make_note(repo, "plain", body="x\n")
    # file notes: id ends in .md
    (repo / "solo.md").write_text("x\n")
    assert read_note_file(store, "solo.md", "a.csv")["error"] == "not_a_folder_note"


# ----------------------------------------------------------------------
# note history + restore (git-backed)


def test_history_and_restore_roundtrip(git_repo: Path) -> None:
    store = NotesStore(git_repo)
    ops = GitOps(repo_path=git_repo)

    def commit_edit(text: str, msg: str) -> None:
        (git_repo / "bikepacking-gear" / "index.md").write_text(text)
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-m", msg)

    commit_edit("version two\n", "second")
    commit_edit("version three\n", "third")

    history = ops.file_history("bikepacking-gear", limit=10)
    assert [h["message"] for h in history[:2]] == ["third", "second"]
    assert all(set(h) == {"commit", "author", "date", "message"} for h in history)

    # restore to the "second" version through the full write flow
    target = history[1]["commit"]
    result = write_flow(
        store,
        ops,
        lambda: (
            ops.restore_file(target, "bikepacking-gear/index.md")
            or {"status": "restored", "note_id": "bikepacking-gear"}
        ),
        lambda _: ["bikepacking-gear/index.md"],
        lambda _: f"Restore bikepacking-gear to {target[:7]}",
    )
    assert result["status"] == "restored"
    assert result["git"]["committed"]
    assert (git_repo / "bikepacking-gear" / "index.md").read_text() == "version two\n"
    # the restore is itself a new commit on top — nothing lost
    newest = ops.file_history("bikepacking-gear", limit=1)[0]
    assert newest["message"].startswith("Restore")


def test_restore_invalid_commit(git_repo: Path) -> None:
    ops = GitOps(repo_path=git_repo)
    err = ops.restore_file("deadbeef" * 5, "bikepacking-gear/index.md")
    assert err is not None and err["error"] == "invalid_commit"


def test_history_non_git_is_empty(store: NotesStore) -> None:
    ops = GitOps(repo_path=store.repo_path)
    assert ops.file_history("bikepacking-gear") == []
