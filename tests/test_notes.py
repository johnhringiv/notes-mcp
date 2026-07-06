"""Tests for notes_mcp.notes (Phase 1: filesystem-only operations)."""

from __future__ import annotations

from pathlib import Path

from conftest import git, make_note

from notes_mcp.notes import NotesStore, split_frontmatter, validate_note_id

# ----------------------------------------------------------------------
# validate_note_id / frontmatter helpers


def test_validate_note_id_accepts_normal_and_nested() -> None:
    assert validate_note_id("bikepacking-gear") is None
    assert validate_note_id("projects/ranger prep") is None
    assert validate_note_id("a1/b2/c3") is None


def test_validate_note_id_rejects_unsafe() -> None:
    for bad in ["", " x", "x ", "../etc", "a/../b", ".hidden", "a/.b", "/abs", "a//b", "a\\b"]:
        assert validate_note_id(bad) is not None, bad


def test_split_frontmatter_roundtrip() -> None:
    fm, body = split_frontmatter("---\ntitle: Hi\ntags: [a, b]\n---\n\n# Hi\n")
    assert fm == {
        "title": "Hi",
        "tags": ["a", "b"],
    }
    assert body == "# Hi\n"


def test_split_frontmatter_absent_or_invalid() -> None:
    assert split_frontmatter("# Just a doc\n") == ({}, "# Just a doc\n")
    assert split_frontmatter("---\nnot: [closed\n") == ({}, "---\nnot: [closed\n")
    # A scalar (non-dict) frontmatter block is not frontmatter.
    text = "---\njust a string\n---\nbody\n"
    assert split_frontmatter(text)[0] == {}


# ----------------------------------------------------------------------
# list_notes


def test_list_notes_shape(store: NotesStore) -> None:
    notes = store.list_notes()["notes"]
    ids = [n["id"] for n in notes]
    assert set(ids) == {"bikepacking-gear", "cycling-analysis", "no-frontmatter"}
    gear = next(n for n in notes if n["id"] == "bikepacking-gear")
    assert gear["title"] == "Bikepacking Gear List"
    assert gear["tags"] == ["bikepacking", "gear"]
    assert gear["path"] == "bikepacking-gear/index.md"
    assert gear["updated_at"]


def test_list_notes_skips_hidden_dirs(store: NotesStore) -> None:
    ids = [n["id"] for n in store.list_notes()["notes"]]
    assert not any(i.startswith(".") for i in ids)


def test_list_notes_title_from_h1_when_no_frontmatter(store: NotesStore) -> None:
    note = next(n for n in store.list_notes()["notes"] if n["id"] == "no-frontmatter")
    assert note["title"] == "Actual Title"


def test_list_notes_filter_matches_id_title_tags(store: NotesStore) -> None:
    assert [n["id"] for n in store.list_notes("bikepack")["notes"]] == ["bikepacking-gear"]
    assert [n["id"] for n in store.list_notes("Cycling")["notes"]] == ["cycling-analysis"]
    assert store.list_notes("zzz-no-match")["notes"] == []


def test_list_notes_includes_nested_notes(store: NotesStore, repo: Path) -> None:
    make_note(repo, "projects/ranger", title="Ranger Prep")
    ids = [n["id"] for n in store.list_notes()["notes"]]
    assert "projects/ranger" in ids
    assert "projects" not in ids  # parent has no index.md


def test_updated_at_from_git_not_mtime(git_store: NotesStore, git_repo: Path) -> None:
    stamp = git_store.updated_at("bikepacking-gear")
    # Git commit timestamps are ISO 8601 with offset (%cI).
    assert "T" in stamp and ("+" in stamp or "Z" in stamp or "-" in stamp[10:])
    # Touching the file does not change the cached git-derived timestamp.
    (git_repo / "bikepacking-gear" / "index.md").touch()
    assert git_store.updated_at("bikepacking-gear") == stamp


def test_updated_at_cache_invalidation(git_store: NotesStore, git_repo: Path) -> None:
    first = git_store.updated_at("bikepacking-gear")
    (git_repo / "bikepacking-gear" / "index.md").write_text("changed\n")
    git(
        git_repo,
        "commit",
        "-am",
        "x",
        extra_env={"GIT_COMMITTER_DATE": "2030-01-02T03:04:05+00:00"},
    )
    assert git_store.updated_at("bikepacking-gear") == first  # cached
    git_store.invalidate_updated_at("bikepacking-gear")
    assert git_store.updated_at("bikepacking-gear") != first


def test_updated_at_mtime_fallback_for_non_git(store: NotesStore) -> None:
    assert store.updated_at("bikepacking-gear")


# ----------------------------------------------------------------------
# read_note


def test_read_note(store: NotesStore) -> None:
    result = store.read_note("cycling-analysis")
    assert result["frontmatter"]["title"] == "Cycling Analysis"
    assert "Analysis notes." in result["content"]
    files = {f["name"]: f for f in result["files"]}
    assert files["ride.fit"]["type"] == "fit"
    assert files["ride.fit"]["size"] > 0
    assert files["scripts/analyze.py"]["type"] == "python"
    assert "index.md" not in files


def test_read_note_missing(store: NotesStore) -> None:
    assert store.read_note("nope")["error"] == "note_not_found"
    assert store.read_note("../escape")["error"] == "invalid_note_id"


def test_read_note_excludes_nested_note_files(store: NotesStore, repo: Path) -> None:
    make_note(repo, "projects", title="Projects")
    make_note(repo, "projects/sub", title="Sub", body="child\n")
    files = [f["name"] for f in store.read_note("projects")["files"]]
    assert files == []  # sub/index.md belongs to the nested note


# ----------------------------------------------------------------------
# search_notes


def test_search_notes_finds_match_with_snippet(store: NotesStore) -> None:
    results = store.search_notes("Tailfin")["results"]
    assert len(results) == 1
    hit = results[0]
    assert hit["note_id"] == "bikepacking-gear"
    assert hit["file"] == "bikepacking-gear/index.md"
    assert "Tailfin rack" in hit["snippet"]
    assert "## Bags" in hit["snippet"]  # ±2 lines of context


def test_search_notes_no_match_and_binary_skip(store: NotesStore) -> None:
    assert store.search_notes("zzz-not-there")["results"] == []
    # binary file contains \x01binary but rg must skip it
    assert store.search_notes("binary")["results"] == []


def test_search_notes_max_results(store: NotesStore, repo: Path) -> None:
    body = "\n".join(f"needle {i}" for i in range(50))
    make_note(repo, "haystack", title="Haystack", body=body)
    assert len(store.search_notes("needle", max_results=5)["results"]) == 5


def test_search_notes_empty_query(store: NotesStore) -> None:
    assert store.search_notes("  ")["error"] == "invalid_query"


def test_search_notes_regex_special_chars_are_literal_safe(store: NotesStore) -> None:
    # A leading dash or regex metachars must not break the rg invocation.
    assert "error" not in store.search_notes("-Tailfin (rack")


# ----------------------------------------------------------------------
# create_note


def test_create_note_default_template(store: NotesStore, repo: Path) -> None:
    result = store.create_note("new-note", "My New Note", tags=["one", "two"])
    assert result["status"] == "created"
    text = (repo / "new-note" / "index.md").read_text()
    fm, body = split_frontmatter(text)
    assert fm["title"] == "My New Note"
    assert fm["tags"] == ["one", "two"]
    assert str(fm["created"])  # today's date
    assert "# My New Note" in body


def test_create_note_named_template(store: NotesStore, repo: Path) -> None:
    store.create_note("standup", "Standup", template="meeting")
    assert "## Agenda" in (repo / "standup" / "index.md").read_text()


def test_create_note_works_without_templates_dir(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    result = NotesStore(bare).create_note("first", "First")
    assert result["status"] == "created"


def test_create_note_errors(store: NotesStore) -> None:
    assert store.create_note("bikepacking-gear", "Dup")["error"] == "note_already_exists"
    assert store.create_note("../bad", "X")["error"] == "invalid_note_id"
    assert store.create_note("ok-id", " ")["error"] == "invalid_title"
    assert store.create_note("ok-id", "X", template="nope")["error"] == "template_not_found"
    assert store.create_note("ok-id", "X", template="../evil")["error"] == "invalid_template"
    assert store.create_note(".templates/x", "X")["error"] == "invalid_note_id"


# ----------------------------------------------------------------------
# append_to_note


def test_append_to_end(store: NotesStore, repo: Path) -> None:
    store.append_to_note("cycling-analysis", "New line.")
    text = (repo / "cycling-analysis" / "index.md").read_text()
    assert text.endswith("Analysis notes.\n\nNew line.\n")


def test_append_under_existing_section(store: NotesStore, repo: Path) -> None:
    store.append_to_note("bikepacking-gear", "- Tailfin cargopack: 410g", section="Bags")
    text = (repo / "bikepacking-gear" / "index.md").read_text()
    assert "- Tailfin rack\n\n- Tailfin cargopack: 410g\n" in text


def test_append_section_before_following_heading(store: NotesStore, repo: Path) -> None:
    make_note(repo, "multi", body="# T\n\n## A\n\nitem a\n\n## B\n\nitem b\n")
    store.append_to_note("multi", "more a", section="A")
    text = (repo / "multi" / "index.md").read_text()
    assert text.index("more a") < text.index("## B")
    assert "item a\n\nmore a\n\n## B" in text


def test_append_creates_missing_section(store: NotesStore, repo: Path) -> None:
    store.append_to_note("cycling-analysis", "content", section="Rides")
    text = (repo / "cycling-analysis" / "index.md").read_text()
    assert text.endswith("## Rides\n\ncontent\n")


def test_append_section_match_is_case_insensitive(store: NotesStore, repo: Path) -> None:
    result = store.append_to_note("bikepacking-gear", "x", section="bags")
    assert "(created)" not in result["appended_to"]


def test_append_ignores_headings_in_code_fences(store: NotesStore, repo: Path) -> None:
    make_note(repo, "fenced", body="# T\n\n```\n## Fake\n```\n\n## Real\n\nbody\n")
    store.append_to_note("fenced", "x", section="Fake")
    text = (repo / "fenced" / "index.md").read_text()
    assert text.endswith("## Fake\n\nx\n")  # created new, didn't match the fenced one


def test_append_errors(store: NotesStore) -> None:
    assert store.append_to_note("nope", "x")["error"] == "note_not_found"
    assert store.append_to_note("cycling-analysis", "")["error"] == "invalid_content"


# ----------------------------------------------------------------------
# edit_note


def test_edit_note_single_match(store: NotesStore, repo: Path) -> None:
    result = store.edit_note("cycling-analysis", "Analysis notes.", "Better notes.")
    assert result["status"] == "edited"
    assert "Better notes." in (repo / "cycling-analysis" / "index.md").read_text()


def test_edit_note_zero_and_multiple_matches(store: NotesStore, repo: Path) -> None:
    assert store.edit_note("cycling-analysis", "absent", "x")["error"] == "no_match"
    make_note(repo, "dup", body="same\nsame\n")
    result = store.edit_note("dup", "same", "x")
    assert result["error"] == "multiple_matches"
    assert result["details"]["count"] == 2


def test_edit_note_invalid_args(store: NotesStore) -> None:
    assert store.edit_note("cycling-analysis", "", "x")["error"] == "invalid_edit"
    assert store.edit_note("cycling-analysis", "a", "a")["error"] == "invalid_edit"


# ----------------------------------------------------------------------
# file notes (hybrid model: bare .md files are notes too)


def file_note_fixture(repo: Path) -> None:
    (repo / "ncc").mkdir(exist_ok=True)
    (repo / "ncc" / "parser_flow.md").write_text(
        "Parser Flow Overview\n\nRecursive descent with precedence climbing.\n"
    )
    (repo / "maeve").mkdir(exist_ok=True)
    (repo / "maeve" / "maeve.md").write_text("# Maeve Language Design Document\n\nBody.\n")
    (repo / "README.md").write_text("# Notes Repo\n")


def test_list_notes_includes_file_notes(store: NotesStore, repo: Path) -> None:
    file_note_fixture(repo)
    notes = {n["id"]: n for n in store.list_notes()["notes"]}
    assert notes["maeve/maeve.md"]["title"] == "Maeve Language Design Document"
    assert notes["maeve/maeve.md"]["path"] == "maeve/maeve.md"
    # No H1 → filename stem fallback
    assert notes["ncc/parser_flow.md"]["title"] == "parser_flow"
    assert notes["README.md"]["title"] == "Notes Repo"
    # Folder notes still listed; their index.md never doubles as a file note
    assert "bikepacking-gear" in notes
    assert not any(i.endswith("/index.md") for i in notes)


def test_read_and_edit_file_note(store: NotesStore, repo: Path) -> None:
    file_note_fixture(repo)
    result = store.read_note("maeve/maeve.md")
    assert "Maeve Language" in result["content"]
    assert result["files"] == []
    assert store.edit_note("maeve/maeve.md", "Body.", "New body.")["status"] == "edited"
    assert "New body." in (repo / "maeve" / "maeve.md").read_text()


def test_append_to_file_note(store: NotesStore, repo: Path) -> None:
    file_note_fixture(repo)
    store.append_to_note("ncc/parser_flow.md", "New paragraph.", section="Ideas")
    assert (repo / "ncc" / "parser_flow.md").read_text().endswith("## Ideas\n\nNew paragraph.\n")


def test_create_file_note(store: NotesStore, repo: Path) -> None:
    result = store.create_note("ncc/new-idea.md", "New Idea", tags=["ncc"])
    assert result == {"status": "created", "note_id": "ncc/new-idea.md", "path": "ncc/new-idea.md"}
    assert (repo / "ncc" / "new-idea.md").is_file()
    assert store.create_note("ncc/new-idea.md", "Dup")["error"] == "note_already_exists"


def test_index_md_is_not_a_valid_note_id(store: NotesStore) -> None:
    assert store.read_note("bikepacking-gear/index.md")["error"] == "invalid_note_id"
    assert validate_note_id("a.md/b") is not None


def test_search_resolves_file_note_ids(store: NotesStore, repo: Path) -> None:
    file_note_fixture(repo)
    results = store.search_notes("precedence climbing")["results"]
    assert results[0]["note_id"] == "ncc/parser_flow.md"
    results = store.search_notes("Tailfin")["results"]
    assert results[0]["note_id"] == "bikepacking-gear"


def test_file_notes_reject_folder_only_operations(store: NotesStore, repo: Path) -> None:
    from notes_mcp.files import add_file_to_note
    from notes_mcp.scripts import list_scripts

    file_note_fixture(repo)
    result = add_file_to_note(store, "maeve/maeve.md", "x.csv", "YQ==")
    assert result["error"] == "not_a_folder_note"
    assert list_scripts(store, "maeve/maeve.md") == {"scripts": []}
