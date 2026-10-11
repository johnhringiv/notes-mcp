"""Per-member access: grant unit tests and a canary leak sweep.

The sweep is the main guarantee. Private content carries a canary string; a
member calls every registered tool with a battery of hostile arguments, and
after every call: no response contains the canary, no file outside their
write grants changed, the tree is clean, and history only grew (HEAD still
descends from the original commit; a read-only member must not move it at
all). The repo is reset after every call so each call sees the original
state (a move early in the sweep can't hide a later one). Tools are
enumerated from the live server, so a new tool joins the sweep
automatically, and the sweep fails if one has no arguments planned.
"""

from __future__ import annotations

import base64
import importlib
import itertools
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import git, make_note

from notes_mcp.access import FULL, Grants
from notes_mcp.members import GitHubUser

CANARY = "ZEBRA7"
# Bob may not change anything here: private dirs plus his read-only area.
PROTECTED_DIRS = (
    "journal",
    "kitchen-private",
    "resources/private",
    "family",
    "resources/recipes",
)

ALICE = "admin:1"  # ADMIN_USERS=alice, GitHub id 1
BOB = "member:bob"  # rw kitchen; ro recipes and family
RITA = "member:rita"  # ro kitchen only
CAROL = "member:carol"  # no such member


# ----------------------------------------------------------------------
# policy units


def test_patterns_cover_the_named_path_and_below() -> None:
    g = Grants.build(read=[], write=["kitchen/**"])
    assert g.can_write("kitchen")
    assert g.can_write("kitchen/spices")
    assert g.can_write("kitchen/a/b/c.md")
    assert not g.can_write("kitchen-private/x.md")
    assert not g.can_write("journal")
    bare = Grants.build(read=["grilling/spice-inventory"], write=[])
    assert bare.can_read("grilling/spice-inventory/index.md")
    assert not bare.can_read("grilling/other")


def test_write_implies_read_but_not_reverse() -> None:
    g = Grants.build(read=["resources/recipes/**"], write=["kitchen/**"])
    assert g.can_read("kitchen/x.md")
    assert g.can_read("resources/recipes/bread.md")
    assert not g.can_write("resources/recipes/bread.md")
    assert FULL.can_write("anything/at/all.md")


@pytest.mark.parametrize(
    "path", ["kitchen/../journal/x.md", "/kitchen/x.md", "kitchen//x", "./kitchen/x", ""]
)
def test_unnormalized_paths_never_match(path: str) -> None:
    assert not Grants.build(read=[], write=["kitchen/**"]).can_read(path)
    assert not FULL.can_read(path)


# ----------------------------------------------------------------------
# server fixture: a git repo with shared and private content


def _commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def shared_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "notes"
    repo.mkdir()
    spices = make_note(repo, "kitchen/spices", title="Spice Inventory", body="- paprika\n")
    (spices / "scripts").mkdir()
    (spices / "scripts" / "count.py").write_text('"""Count spices."""\nprint("3 spices")\n')
    # A script in bob's own folder that reads private files: why run_script
    # needs full access (bob could upload one like it with add_file_to_note).
    (spices / "scripts" / "peek.py").write_text(
        'print(open("../../journal/diary/index.md").read())\n'
    )
    make_note(repo, "family/calendar", title="Calendar", body="- dentist\n")
    (repo / "kitchen" / "shopping.md").write_text("# Shopping\n\n- paprika\n")
    (repo / "resources" / "recipes").mkdir(parents=True)
    (repo / "resources" / "recipes" / "bread.md").write_text("# Bread\n\nflour, paprika\n")
    git(repo, "init", "-b", "main")
    shas = {"seed": _commit_all(repo, "seed")}

    diary = make_note(
        repo, "journal/diary", title=f"Diary {CANARY}", body=f"Private {CANARY} body.\n"
    )
    (diary / "secret.txt").write_text(f"{CANARY} file\n")
    (diary / "scripts").mkdir()
    (diary / "scripts" / "leak.py").write_text(f'"""{CANARY} script."""\nprint("{CANARY}")\n')
    make_note(repo, f"journal/{CANARY.lower()}-hidden", title="Hidden", body="nothing\n")
    (repo / "kitchen-private").mkdir()
    (repo / "kitchen-private" / "x.md").write_text(f"# Prefix trap {CANARY}\n")
    (repo / "resources" / "private").mkdir()
    (repo / "resources" / "private" / "plan.md").write_text(f"# Plan {CANARY}\n")
    with (repo / "family" / "calendar" / "index.md").open("a") as fh:
        fh.write("- vet\n")  # history for restore_note to (wrongly) roll back
    shas["private"] = _commit_all(repo, f"Private stuff {CANARY}")

    # One commit touching both a shared and a private note: its message must
    # not reach bob through note_history on the shared note.
    with (repo / "kitchen" / "shopping.md").open("a") as fh:
        fh.write("- cumin\n")
    with (diary / "index.md").open("a") as fh:
        fh.write(f"more {CANARY}\n")
    shas["mixed"] = _commit_all(repo, f"Tidy shopping and {CANARY} diary")
    return repo, shas


@pytest.fixture
def server(shared_repo: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    repo, _ = shared_repo
    for var in ("NOTES_REPO_URL", "GITHUB_TOKEN", "GITHUB_TOKEN_FILE", "GITHUB_ALLOWED_LOGIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOTES_REPO_PATH", str(repo))
    monkeypatch.setenv("PUBLIC_URL", "http://localhost")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "gh-client")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "gh-secret")
    monkeypatch.setenv("ADMIN_USERS", "alice")
    monkeypatch.setenv("OAUTH_STATE_DIR", str(repo.parent / "state"))
    import notes_mcp.server

    srv = importlib.reload(notes_mcp.server)
    assert srv.members.login_github(GitHubUser(1, "alice")) == ALICE
    srv.members.add_member("bob")
    srv.members.set_grant("bob", "kitchen/**", "rw")
    srv.members.set_grant("bob", "resources/recipes", "ro")
    srv.members.set_grant("bob", "family", "ro")
    srv.members.add_member("rita")
    srv.members.set_grant("rita", "kitchen", "ro")
    return srv


def as_user(server: ModuleType, monkeypatch: pytest.MonkeyPatch, subject: str) -> None:
    monkeypatch.setattr(server, "_current_subject", lambda: subject)


async def call(server: ModuleType, tool: str, args: dict[str, Any]) -> str:
    """Everything the caller could observe, as one string."""
    try:
        _, structured = await server.mcp.call_tool(tool, args)
        return json.dumps(structured)
    except Exception as exc:  # validation errors etc. still reach the client
        return f"{type(exc).__name__}: {exc}"


# ----------------------------------------------------------------------


def test_every_tool_is_guarded(server: ModuleType) -> None:
    tools = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert tools <= server._GUARDED


async def test_restricted_user_sees_shared_content(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: bob's grants actually work (the sweep isn't vacuous)."""
    as_user(server, monkeypatch, BOB)
    listing = json.loads(await call(server, "list_notes", {}))
    assert {n["id"] for n in listing["notes"]} == {
        "kitchen/spices",
        "kitchen/shopping.md",
        "family/calendar",
    }
    hits = json.loads(await call(server, "search_notes", {"query": "paprika"}))["results"]
    assert {h["file"] for h in hits} == {
        "kitchen/spices/index.md",
        "kitchen/shopping.md",
        "resources/recipes/bread.md",
    }
    resources = json.loads(await call(server, "list_resources", {}))["resources"]
    assert [r["id"] for r in resources] == ["recipes/bread.md"]
    history = json.loads(await call(server, "note_history", {"note_id": "kitchen/shopping.md"}))
    assert history["history"][0]["message"].startswith("(message hidden")
    assert history["history"][1]["message"] == "seed"
    result = json.loads(
        await call(
            server, "append_to_note", {"note_id": "kitchen/shopping.md", "content": "- salt"}
        )
    )
    assert result["git"]["committed"]
    log = subprocess.run(
        ["git", "-C", str(server.settings.notes_repo_path), "log", "-1", "--format=%an"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log == "bob"


async def test_grant_changes_apply_to_the_next_call(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new token needed: grants are re-read on every call."""
    as_user(server, monkeypatch, RITA)
    denied = json.loads(
        await call(server, "append_to_note", {"note_id": "kitchen/shopping.md", "content": "- x"})
    )
    assert denied["error"] == "forbidden"
    server.members.set_grant("rita", "kitchen", "rw")
    allowed = json.loads(
        await call(server, "append_to_note", {"note_id": "kitchen/shopping.md", "content": "- x"})
    )
    assert allowed["git"]["committed"]
    server.members.remove_grant("rita", "kitchen")
    gone = json.loads(await call(server, "read_note", {"note_id": "kitchen/shopping.md"}))
    assert gone["error"] == "note_not_found"


async def test_members_cannot_run_scripts_even_in_their_folder(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.members.set_grant("bob", "**", "rw")  # everything, still not an admin
    as_user(server, monkeypatch, BOB)
    result = json.loads(
        await call(server, "run_script", {"note_id": "kitchen/spices", "script_name": "count.py"})
    )
    assert result["error"] == "forbidden"


async def test_full_user_sees_private_content(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canary is findable by someone allowed to see it."""
    as_user(server, monkeypatch, ALICE)
    assert CANARY in await call(server, "search_notes", {"query": CANARY})
    assert CANARY in await call(server, "note_history", {"note_id": "kitchen/shopping.md"})
    assert "3 spices" in await call(
        server, "run_script", {"note_id": "kitchen/spices", "script_name": "count.py"}
    )


async def test_unknown_member_gets_nothing(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_user(server, monkeypatch, CAROL)
    assert json.loads(await call(server, "list_notes", {}))["notes"] == []
    assert json.loads(await call(server, "search_notes", {"query": "paprika"}))["results"] == []
    denied = json.loads(await call(server, "read_note", {"note_id": "kitchen/spices"}))
    assert denied["error"] == "note_not_found"


async def test_symlink_does_not_carry_a_share(
    server: ModuleType,
    shared_repo: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _ = shared_repo
    (repo / "kitchen" / "link.md").symlink_to("../journal/diary/index.md")
    (repo / "kitchen" / "linkdir").symlink_to("../journal/diary")
    (repo / "resources" / "recipes" / "link.md").symlink_to("../private/plan.md")
    as_user(server, monkeypatch, BOB)
    for tool, args in [
        ("read_note", {"note_id": "kitchen/link.md"}),
        ("read_note", {"note_id": "kitchen/linkdir"}),
        ("read_note_file", {"note_id": "kitchen/linkdir", "filename": "secret.txt"}),
        ("read_resource", {"resource_id": "recipes/link.md"}),
        ("list_notes", {}),
        ("list_resources", {}),
        ("search_notes", {"query": CANARY}),
        ("note_history", {"note_id": "kitchen/link.md"}),
    ]:
        observed = await call(server, tool, args)
        assert CANARY.lower() not in observed.lower(), (tool, observed[:300])
    as_user(server, monkeypatch, ALICE)
    assert CANARY in await call(server, "read_note", {"note_id": "kitchen/link.md"})


def _snapshot(repo: Path) -> dict[str, bytes]:
    out = {}
    for d in PROTECTED_DIRS:
        for p in sorted((repo / d).rglob("*")):
            if p.is_file():
                out[p.relative_to(repo).as_posix()] = p.read_bytes()
    return out


def _arg_candidates(shas: dict[str, str]) -> dict[str, list[Any]]:
    return {
        "note_id": [
            "kitchen/spices",
            "kitchen/shopping.md",
            "journal/diary",
            "journal",
            "kitchen/../journal/diary",
            "kitchen-private/x.md",
            "journal/new-note",
            "family/calendar",
        ],
        "new_note_id": ["kitchen/moved", "journal/stolen", "family/stolen", "kitchen/moved.md"],
        "resource_id": ["recipes/bread.md", "private/plan.md", "recipes/../private/plan.md"],
        "filename": ["secret.txt", "scripts/leak.py", "../../journal/diary/secret.txt"],
        "query": [CANARY, "paprika", "Private"],
        "filter": [None, "journal", "diary", "hidden"],
        "commit": [shas["seed"], shas["private"], shas["mixed"]],
        "script_name": ["leak.py", "count.py", "peek.py"],
        "append": [False, True],
    }


DEFAULTS: dict[str, Any] = {
    "title": "t",
    "content": "x",
    "old_str": "paprika",
    "new_str": "smoked paprika",
    "content_b64": base64.b64encode(b"hi").decode(),
    "section": None,
}


def _git_out(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.mark.parametrize("subject", [BOB, RITA])
async def test_canary_sweep(
    server: ModuleType,
    shared_repo: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
) -> None:
    repo, shas = shared_repo
    before = _snapshot(repo)
    head = shas["mixed"]
    candidates = _arg_candidates(shas)
    as_user(server, monkeypatch, subject)

    calls = 0
    for tool in server.mcp._tool_manager.list_tools():
        params = tool.parameters["properties"]
        required = set(tool.parameters.get("required", []))
        varying = [p for p in params if p in candidates]
        unplanned = [p for p in required if p not in candidates and p not in DEFAULTS]
        assert not unplanned, f"plan sweep arguments for {tool.name}: {unplanned}"
        fixed = {p: DEFAULTS[p] for p in params if p in DEFAULTS and p in required}
        for combo in itertools.product(*(candidates[p] for p in varying)):
            args = {**fixed, **{p: v for p, v in zip(varying, combo, strict=True) if v is not None}}
            observed = await call(server, tool.name, args)
            assert CANARY.lower() not in observed.lower(), (tool.name, args, observed[:500])
            assert _snapshot(repo) == before, (tool.name, args, "changed a protected file")
            assert _git_out(repo, "status", "--porcelain").stdout == "", (tool.name, args)
            assert _git_out(repo, "merge-base", "--is-ancestor", head, "HEAD").returncode == 0, (
                tool.name,
                args,
                "history was rewritten",
            )
            if subject == RITA:
                now = _git_out(repo, "rev-parse", "HEAD").stdout.strip()
                assert now == head, (tool.name, args, "read-only member committed")
            git(repo, "reset", "-q", "--hard", head)
            git(repo, "clean", "-qfdx")
            server.store.refresh_all_updated_at()
            calls += 1

    assert calls > 100


async def test_request_without_a_user_sees_nothing(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With auth on, a call that somehow carries no principal fails closed."""
    monkeypatch.setattr(server, "_current_subject", lambda: None)
    assert json.loads(await call(server, "list_notes", {}))["notes"] == []
    denied = json.loads(await call(server, "read_note", {"note_id": "kitchen/spices"}))
    assert denied["error"] == "note_not_found"
