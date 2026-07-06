"""Tests for notes_mcp.git_ops: bare repo as remote, working clone as local."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git

from notes_mcp.git_ops import GitError, GitOps, write_flow
from notes_mcp.notes import NotesStore


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare 'origin' seeded with one note on main."""
    seed = tmp_path / "seed"
    (seed / "gear").mkdir(parents=True)
    (seed / "gear" / "index.md").write_text("---\ntitle: Gear\n---\n\n# Gear\n")
    git(seed, "init", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(seed, "clone", "--bare", str(seed), str(bare))
    return bare


def clone(remote: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], check=True, capture_output=True)
    return dest


@pytest.fixture
def local(remote: Path, tmp_path: Path) -> Path:
    return clone(remote, tmp_path / "local")


@pytest.fixture
def ops(local: Path) -> GitOps:
    return GitOps(repo_path=local, author_name="Claude MCP", author_email="claude@test")


def test_detects_branch_and_remote(ops: GitOps) -> None:
    assert ops.enabled and ops.has_remote
    assert ops.branch == "main"


def test_commit_lands_on_remote_with_author(local: Path, remote: Path, ops: GitOps) -> None:
    (local / "gear" / "index.md").write_text("changed\n")
    result = ops.commit_and_push(["gear/index.md"], "Edit gear")
    assert result["committed"] and result["pushed"]
    log = git_out(remote, "log", "-1", "--format=%s|%an|%ae")
    assert log.strip() == "Edit gear|Claude MCP|claude@test"
    assert result["commit"] == git_out(remote, "rev-parse", "HEAD").strip()


def test_no_change_means_no_commit(ops: GitOps, remote: Path) -> None:
    before = git_out(remote, "rev-parse", "HEAD")
    result = ops.commit_and_push(["gear/index.md"], "noop")
    assert result == {"committed": False, "pushed": False, "commit": None}
    assert git_out(remote, "rev-parse", "HEAD") == before


def test_push_rejection_pulls_and_retries(
    local: Path, remote: Path, tmp_path: Path, ops: GitOps
) -> None:
    # Advance the remote behind our back with a non-conflicting change.
    other = clone(remote, tmp_path / "other")
    (other / "unrelated.md").write_text("hi\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "other device")
    git(other, "push", "origin", "main")

    (local / "gear" / "index.md").write_text("local change\n")
    result = ops.commit_and_push(["gear/index.md"], "Edit gear")
    assert result["pushed"]
    subjects = git_out(remote, "log", "--format=%s")
    assert "Edit gear" in subjects and "other device" in subjects


def test_pull_conflict_aborts_cleanly(
    local: Path, remote: Path, tmp_path: Path, ops: GitOps
) -> None:
    other = clone(remote, tmp_path / "other")
    (other / "gear" / "index.md").write_text("remote version\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote edit")
    git(other, "push", "origin", "main")

    # Conflicting local commit, then pull.
    (local / "gear" / "index.md").write_text("local version\n")
    git(local, "add", "-A")
    git(local, "commit", "-m", "local edit")
    with pytest.raises(GitError) as exc_info:
        ops.pull()
    assert exc_info.value.payload["error"] == "merge_conflict"
    # Tree is left clean (rebase aborted), not mid-rebase.
    assert not (local / ".git" / "rebase-merge").exists()
    assert git_out(local, "status", "--porcelain") == ""


def test_no_remote_commits_locally(tmp_path: Path) -> None:
    repo = tmp_path / "standalone"
    (repo / "n").mkdir(parents=True)
    (repo / "n" / "index.md").write_text("x\n")
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    ops = GitOps(repo_path=repo)
    (repo / "n" / "index.md").write_text("y\n")
    result = ops.commit_and_push(["n/index.md"], "Edit n")
    assert result["committed"] and not result["pushed"]
    ops.pull()  # no-op, must not raise


def test_non_git_dir_is_noop(tmp_path: Path) -> None:
    ops = GitOps(repo_path=tmp_path)
    assert not ops.enabled
    ops.pull()
    assert ops.commit_and_push(["x"], "m") == {"committed": False, "pushed": False, "commit": None}


def test_hard_reset_if_dirty(local: Path, ops: GitOps) -> None:
    assert ops.hard_reset_if_dirty() is False
    (local / "gear" / "index.md").write_text("scratch\n")
    (local / "gear" / "leftover.tmp").write_text("junk\n")
    assert ops.hard_reset_if_dirty() is True
    assert "# Gear" in (local / "gear" / "index.md").read_text()
    assert not (local / "gear" / "leftover.tmp").exists()


# ----------------------------------------------------------------------
# write_flow integration


def test_write_flow_append_commits_and_pushes(local: Path, remote: Path, ops: GitOps) -> None:
    store = NotesStore(local)
    result = write_flow(
        store,
        ops,
        lambda: store.append_to_note("gear", "- Tailfin cargopack: 410g"),
        lambda _: ["gear/index.md"],
        lambda _: "Append to gear",
    )
    assert result["status"] == "appended"
    assert result["git"]["pushed"]
    assert "Tailfin cargopack" in git_out(remote, "show", "HEAD:gear/index.md")


def test_write_flow_tool_error_skips_commit(local: Path, remote: Path, ops: GitOps) -> None:
    store = NotesStore(local)
    before = git_out(remote, "rev-parse", "HEAD")
    result = write_flow(
        store,
        ops,
        lambda: store.edit_note("gear", "absent text", "x"),
        lambda _: ["gear/index.md"],
        lambda _: "Edit gear",
    )
    assert result["error"] == "no_match"
    assert git_out(remote, "rev-parse", "HEAD") == before


def test_write_flow_conflict_returns_structured_error(
    local: Path, remote: Path, tmp_path: Path, ops: GitOps
) -> None:
    other = clone(remote, tmp_path / "other")
    (other / "gear" / "index.md").write_text("remote version\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote edit")
    git(other, "push", "origin", "main")

    (local / "gear" / "index.md").write_text("local version\n")
    git(local, "add", "-A")
    git(local, "commit", "-m", "local edit")

    store = NotesStore(local)
    result = write_flow(
        store,
        ops,
        lambda: store.append_to_note("gear", "x"),
        lambda _: ["gear/index.md"],
        lambda _: "Append to gear",
    )
    assert result["error"] == "merge_conflict"


def test_head_valid_detects_interrupted_clone(remote: Path, tmp_path: Path) -> None:
    """A clone killed mid-checkout leaves an unborn HEAD; clone() must recover."""
    broken = tmp_path / "broken"
    broken.mkdir()
    git(broken, "init", "-b", "main")
    git(broken, "remote", "add", "origin", str(remote))
    # simulate the mid-checkout state: files staged on an unborn branch
    (broken / "gear").mkdir()
    (broken / "gear" / "index.md").write_text("partial\n")
    git(broken, "add", "-A")

    ops = GitOps(repo_path=broken)
    assert ops.enabled
    assert ops.head_valid() is False

    ops.clone(str(remote))  # wipes debris and re-clones
    assert ops.head_valid() is True
    assert "# Gear" in (broken / "gear" / "index.md").read_text()
    # and writes work again
    (broken / "gear" / "index.md").write_text("fixed\n")
    result = ops.commit_and_push(["gear/index.md"], "Edit gear")
    assert result["pushed"]


def test_head_valid_on_healthy_clone(ops: GitOps) -> None:
    assert ops.head_valid() is True
