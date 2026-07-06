"""Git pull/commit/push wrapper for write tools.

Pattern: every write does pull --rebase → mutate → add → commit →
push, serialized by a single process-wide asyncio lock held by the caller
(server.py). Pull conflicts abort the operation with a structured error and
leave the tree clean; a rejected push is retried once after re-pulling.

Two degraded modes make local dev painless and are safe in production:
- not a git repo at all → all git steps are no-ops (Phase 1 behavior);
- git repo without an "origin" remote → commit locally, skip pull/push.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from notes_mcp.formatting import format_markdown
from notes_mcp.notes import NotesStore

logger = logging.getLogger("notes_mcp.git")

# GIT_ASKPASS script: git calls it with "Username for ..." / "Password for ..."
# prompts. Username is a fixed placeholder (GitHub PATs ignore it); password
# comes from GIT_PASSWORD, set only on the git subprocess's environment.
# Credentials never touch .git/config or the remote URL.
_ASKPASS_SCRIPT = """\
#!/bin/sh
case "$1" in
  Username*) echo "x-access-token" ;;
  *) printenv GIT_PASSWORD ;;
esac
"""


class GitError(Exception):
    """A git step failed; carries the structured error dict for the tool reply."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload["error"])
        self.payload = payload


@dataclass
class GitOps:
    repo_path: Path
    branch: str = "main"
    author_name: str = "Claude MCP"
    author_email: str = "claude-mcp@users.noreply.github.com"
    token: str | None = None
    _askpass: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.repo_path = self.repo_path.resolve()
        self._probe()

    def _probe(self) -> None:
        self.enabled = (self.repo_path / ".git").exists()
        self.has_remote = self.enabled and self._run("remote", "get-url", "origin").returncode == 0
        if self.enabled:
            detected = self._detect_branch()
            if detected:
                self.branch = detected

    def _detect_branch(self) -> str | None:
        result = self._run("rev-parse", "--abbrev-ref", "HEAD")
        name = result.stdout.strip()
        return name if result.returncode == 0 and name != "HEAD" else None

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if self.token is not None:
            if self._askpass is None:
                fd, path = tempfile.mkstemp(prefix="askpass-", suffix=".sh")
                with os.fdopen(fd, "w") as fh:
                    fh.write(_ASKPASS_SCRIPT)
                os.chmod(path, stat.S_IRWXU)
                self._askpass = Path(path)
            env["GIT_ASKPASS"] = str(self._askpass)
            env["GIT_PASSWORD"] = self.token
        return env

    def _run(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_path),
                "-c",
                f"user.name={self.author_name}",
                "-c",
                f"user.email={self.author_email}",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._git_env(),
        )

    def head_valid(self) -> bool:
        """True if HEAD points at a real commit.

        A clone killed mid-checkout (e.g. `docker rm -f` during startup)
        leaves `.git` with an unborn branch: reads work off the checked-out
        files, but every pull/commit fails. Startup uses this to detect the
        state and re-clone.
        """
        if not self.enabled:
            return False
        return self._run("rev-parse", "--verify", "HEAD").returncode == 0

    def clone(self, url: str) -> None:
        """Clone `url` into repo_path (used at container startup).

        Any existing contents (e.g. the debris of an interrupted clone) are
        removed first — a failed `git clone` into a pre-existing directory
        does not clean up after itself.
        """
        self.repo_path.mkdir(parents=True, exist_ok=True)
        for child in self.repo_path.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        result = subprocess.run(
            ["git", "clone", "--branch", self.branch, url, str(self.repo_path)],
            capture_output=True,
            text=True,
            timeout=600,
            env=self._git_env(),
        )
        if result.returncode != 0:
            raise GitError(
                {
                    "error": "clone_failed",
                    "details": {"url": url, "stderr": result.stderr.strip()[:1000]},
                }
            )
        self._probe()

    def _check(self, result: subprocess.CompletedProcess[str], step: str) -> None:
        if result.returncode != 0:
            raise GitError(
                {
                    "error": "git_error",
                    "details": {"step": step, "stderr": result.stderr.strip()[:1000]},
                }
            )

    # ------------------------------------------------------------------

    def pull(self) -> None:
        """git pull --rebase origin <branch>. Conflict → abort rebase, GitError."""
        if not self.has_remote:
            return
        result = self._run("pull", "--rebase", "origin", self.branch, timeout=120)
        if result.returncode != 0:
            self._run("rebase", "--abort")
            raise GitError(
                {
                    "error": "merge_conflict",
                    "details": {
                        "message": "pull --rebase failed; resolve on desktop",
                        "stderr": result.stderr.strip()[:1000],
                    },
                }
            )

    def commit_and_push(self, paths: list[str], message: str) -> dict[str, Any]:
        """Stage paths, commit, push (with one pull+retry on rejection).

        Returns {"committed": bool, "pushed": bool, "commit": sha|None}.
        """
        if not self.enabled:
            return {"committed": False, "pushed": False, "commit": None}

        self._check(self._run("add", "--", *paths), "add")

        status = self._run("status", "--porcelain")
        if not status.stdout.strip():
            return {"committed": False, "pushed": False, "commit": None}

        self._check(
            self._run(
                "commit",
                "-m",
                message,
                f"--author={self.author_name} <{self.author_email}>",
            ),
            "commit",
        )
        sha = self._run("rev-parse", "HEAD").stdout.strip()

        if not self.has_remote:
            return {"committed": True, "pushed": False, "commit": sha}

        for attempt in (1, 2):
            push = self._run("push", "origin", self.branch, timeout=120)
            if push.returncode == 0:
                return {"committed": True, "pushed": True, "commit": sha}
            logger.warning(
                "push rejected",
                extra={"attempt": attempt, "stderr": push.stderr.strip()[:500]},
            )
            if attempt == 1:
                self.pull()  # raises merge_conflict if the rebase fails
                sha = self._run("rev-parse", "HEAD").stdout.strip()
        raise GitError(
            {
                "error": "push_failed",
                "details": {
                    "message": "push rejected twice; commit is local only",
                    "stderr": push.stderr.strip()[:1000],
                },
            }
        )

    def hard_reset_if_dirty(self) -> bool:
        """Reset a dirty tree to HEAD (startup recovery). Returns True if reset."""
        if not self.enabled:
            return False
        status = self._run("status", "--porcelain")
        if not status.stdout.strip():
            return False
        logger.warning(
            "dirty working tree at startup; hard-resetting",
            extra={"status": status.stdout.strip()[:2000]},
        )
        self._check(self._run("reset", "--hard", "HEAD"), "reset")
        self._check(self._run("clean", "-fd"), "clean")
        return True


def write_flow(
    store: NotesStore,
    git_ops: GitOps,
    mutate: Callable[[], dict[str, Any]],
    commit_paths: Callable[[dict[str, Any]], list[str]],
    message: Callable[[dict[str, Any]], str],
    formatter: str | None = None,
) -> dict[str, Any]:
    """pull → mutate → format markdown → commit/push.

    Caller must hold the process write lock. Formatting is best-effort and
    runs on the changed .md files only, so committed markdown matches the
    notes repo's prettier style (its pre-commit hook can't fire here).
    """
    try:
        git_ops.pull()
        store.refresh_all_updated_at()
        result = mutate()
        if "error" in result:
            return result
        paths = commit_paths(result)
        formatted = format_markdown(git_ops.repo_path, paths, formatter)
        git_result = git_ops.commit_and_push(paths, message(result))
        return {**result, "git": {**git_result, "formatted": formatted}}
    except GitError as exc:
        # The tool-call log line only carries the status; record the git
        # stderr here so failures are diagnosable from docker logs.
        logger.warning("write failed", extra={"git_error": exc.payload})
        return exc.payload
