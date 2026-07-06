"""Tests for notes_mcp.scripts: success, failure, timeout, output caps, hangs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from notes_mcp.notes import NotesStore
from notes_mcp.scripts import STDOUT_CAP, list_scripts, run_script

pytestmark = pytest.mark.asyncio


def add_script(repo: Path, note_id: str, name: str, content: str) -> Path:
    scripts_dir = repo / note_id / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    path = scripts_dir / name
    path.write_text(content, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# list_scripts


async def test_list_scripts_descriptions(store: NotesStore) -> None:
    result = list_scripts(store, "cycling-analysis")
    assert result["scripts"] == [{"name": "analyze.py", "description": "Analyze a FIT file."}]


async def test_list_scripts_sh_comment_description(store: NotesStore, repo: Path) -> None:
    add_script(
        repo, "cycling-analysis", "sync.sh", "#!/bin/bash\n# Sync rides\n# from device\necho hi\n"
    )
    result = list_scripts(store, "cycling-analysis")
    names = {s["name"]: s["description"] for s in result["scripts"]}
    assert names["sync.sh"] == "Sync rides from device"


async def test_list_scripts_no_dir_and_missing_note(store: NotesStore) -> None:
    assert list_scripts(store, "bikepacking-gear") == {"scripts": []}
    assert list_scripts(store, "nope")["error"] == "note_not_found"


# ----------------------------------------------------------------------
# run_script


async def test_run_script_success_cwd_and_args(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "hello.py",
        "import os, sys\nprint(os.path.basename(os.getcwd()))\nprint(sys.argv[1])\n",
    )
    result = await run_script(store, "cycling-analysis", "hello.py", args=["muddy-onion.fit"])
    assert result["exit_code"] == 0
    assert result["stdout"] == "cycling-analysis\nmuddy-onion.fit\n"
    assert result["stderr"] == ""
    assert result["duration_seconds"] >= 0


async def test_run_script_shebang_beats_extension(store: NotesStore, repo: Path) -> None:
    add_script(
        repo, "cycling-analysis", "weird.txt", "#!/usr/bin/env python3\nprint('via shebang')\n"
    )
    result = await run_script(store, "cycling-analysis", "weird.txt")
    assert result["exit_code"] == 0
    assert result["stdout"] == "via shebang\n"


async def test_run_script_failure_exit_code_and_stderr(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "boom.py",
        "import sys\nsys.stderr.write('bad fit file\\n')\nsys.exit(3)\n",
    )
    result = await run_script(store, "cycling-analysis", "boom.py")
    assert result["exit_code"] == 3
    assert "bad fit file" in result["stderr"]


async def test_run_script_timeout_kills_hanging_script(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "hang.py",
        "import time\nprint('starting', flush=True)\ntime.sleep(60)\n",
    )
    start = time.monotonic()
    result = await run_script(store, "cycling-analysis", "hang.py", timeout_seconds=1)
    assert result["error"] == "timeout"
    assert time.monotonic() - start < 10
    assert "starting" in result["details"]["stdout"]  # partial output preserved


async def test_run_script_timeout_kills_children_too(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "spawner.sh",
        "#!/bin/bash\nsleep 60 &\nCHILD=$!\necho $CHILD\nwait\n",
    )
    result = await run_script(store, "cycling-analysis", "spawner.sh", timeout_seconds=1)
    assert result["error"] == "timeout"
    child_pid = int(result["details"]["stdout"].strip())
    # The whole process group was killed, so the background sleep is gone.
    time.sleep(0.2)
    assert not Path(f"/proc/{child_pid}").exists()


async def test_run_script_stdout_cap_truncates(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "spam.py",
        "import sys\nfor _ in range(3000):\n    sys.stdout.write('x' * 1000 + '\\n')\n",
    )
    result = await run_script(store, "cycling-analysis", "spam.py")
    assert result["exit_code"] == 0  # process was NOT killed, just truncated
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) == STDOUT_CAP


async def test_run_script_unrecognized_interpreter(store: NotesStore, repo: Path) -> None:
    add_script(repo, "cycling-analysis", "data.csv", "a,b\n1,2\n")
    result = await run_script(store, "cycling-analysis", "data.csv")
    assert result["error"] == "unrecognized_interpreter"


async def test_run_script_validation_errors(store: NotesStore) -> None:
    assert (await run_script(store, "nope", "x.py"))["error"] == "note_not_found"
    bad = await run_script(store, "cycling-analysis", "../../../etc/passwd")
    assert bad["error"] == "invalid_script_name"
    missing = await run_script(store, "cycling-analysis", "ghost.py")
    assert missing["error"] == "script_not_found"


async def test_run_script_progress_callback_fires(store: NotesStore, repo: Path) -> None:
    add_script(
        repo,
        "cycling-analysis",
        "slow.py",
        "import time\nprint('phase one', flush=True)\ntime.sleep(2.2)\n",
    )
    events: list[tuple[str, float]] = []

    async def on_progress(message: str, elapsed: float) -> None:
        events.append((message, elapsed))

    import notes_mcp.scripts as scripts_mod

    original = scripts_mod.KEEPALIVE_SECONDS
    scripts_mod.KEEPALIVE_SECONDS = 1
    try:
        result = await run_script(
            store, "cycling-analysis", "slow.py", timeout_seconds=30, on_progress=on_progress
        )
    finally:
        scripts_mod.KEEPALIVE_SECONDS = original
    assert result["exit_code"] == 0
    assert len(events) >= 1
    assert events[0][0] == "phase one"  # latest stdout line rides along


async def test_scripts_do_not_inherit_secrets(
    store: NotesStore, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-pat")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "super-secret-oauth")
    monkeypatch.setenv("TUNNEL_TOKEN", "super-secret-tunnel")
    monkeypatch.setenv("NOTES_REPO_URL", "https://example.com/x.git")  # non-secret survives
    add_script(
        repo,
        "cycling-analysis",
        "envdump.py",
        "import os\nprint(sorted(k for k in os.environ if 'GITHUB' in k or 'TUNNEL' in k))\n"
        "print(os.environ.get('NOTES_REPO_URL'))\n",
    )
    result = await run_script(store, "cycling-analysis", "envdump.py")
    assert result["exit_code"] == 0
    assert "super-secret" not in result["stdout"]
    assert "GITHUB_TOKEN" not in result["stdout"]
    assert "https://example.com/x.git" in result["stdout"]
