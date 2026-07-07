"""Tests for the resource classification (resources.py + notes-side integration)."""

from __future__ import annotations

from pathlib import Path

from notes_mcp.notes import NotesStore
from notes_mcp.resources import add_resource, list_resources, read_resource


def seed_resource(store: NotesStore, body: str = "") -> None:
    content = (
        "---\ntitle: Austral Spec\nsource: https://austral-lang.org/spec\n"
        "retrieved: 2026-07-06\nfidelity: verbatim\n---\n\n# Austral Spec\n\n" + body
    )
    assert add_resource(store, "maeve/austral-spec.md", content)["status"] == "added"


def test_add_list_roundtrip(store: NotesStore) -> None:
    seed_resource(store)
    listing = list_resources(store)["resources"]
    assert len(listing) == 1
    entry = listing[0]
    assert entry["id"] == "maeve/austral-spec.md"
    assert entry["title"] == "Austral Spec"
    assert entry["source"] == "https://austral-lang.org/spec"
    assert entry["fidelity"] == "verbatim"
    assert entry["retrieved"] == "2026-07-06"


def test_append_chunks_and_total_lines(store: NotesStore, repo: Path) -> None:
    seed_resource(store)
    r1 = add_resource(
        store, "maeve/austral-spec.md", "chunk two line 1\nchunk two line 2", append=True
    )
    assert r1["status"] == "appended"
    r2 = add_resource(store, "maeve/austral-spec.md", "chunk three", append=True)
    assert r2["total_lines"] == r1["total_lines"] + 1
    text = (repo / "resources" / "maeve" / "austral-spec.md").read_text()
    assert text.endswith("chunk two line 1\nchunk two line 2\nchunk three\n")


def test_replace_without_append(store: NotesStore, repo: Path) -> None:
    seed_resource(store)
    result = add_resource(store, "maeve/austral-spec.md", "fresh content")
    assert result["status"] == "updated"
    assert (repo / "resources" / "maeve" / "austral-spec.md").read_text() == "fresh content\n"


def test_append_requires_existing(store: NotesStore) -> None:
    assert add_resource(store, "maeve/nope.md", "x", append=True)["error"] == "resource_not_found"


def test_validation(store: NotesStore) -> None:
    assert add_resource(store, "../evil.md", "x")["error"] == "invalid_resource_id"
    assert add_resource(store, "maeve/spec.pdf", "x")["error"] == "invalid_resource_id"
    assert add_resource(store, ".hidden/spec.md", "x")["error"] == "invalid_resource_id"
    assert add_resource(store, "maeve/spec.md", "  ")["error"] == "invalid_content"
    assert read_resource(store, "maeve/ghost.md")["error"] == "resource_not_found"


def test_read_resource_paging(store: NotesStore) -> None:
    body = "\n".join(f"line {i}" for i in range(1, 501))
    add_resource(store, "book.md", body)
    page = read_resource(store, "book.md", start_line=101, limit=50)
    assert page["start_line"] == 101 and page["end_line"] == 150
    assert page["total_lines"] == 500
    assert page["content"].splitlines()[0] == "line 101"
    tail = read_resource(store, "book.md", start_line=499, limit=50)
    assert tail["end_line"] == 500


def test_resources_are_not_notes(store: NotesStore) -> None:
    seed_resource(store)
    ids = [n["id"] for n in store.list_notes()["notes"]]
    assert not any(i.startswith("resources/") for i in ids)


def test_reserved_dir_blocks_note_tools(store: NotesStore) -> None:
    assert store.create_note("resources/x", "X")["error"] == "invalid_note_id"
    assert store.create_note("resources/x.md", "X")["error"] == "invalid_note_id"
    assert store.move_note("bikepacking-gear", "resources/gear")["error"] == "invalid_note_id"


def test_search_attributes_resources(store: NotesStore) -> None:
    seed_resource(store, "linear types are affine plus relevance\n")
    hits = store.search_notes("linear types are affine")["results"]
    assert len(hits) == 1
    assert hits[0]["resource_id"] == "maeve/austral-spec.md"
    assert hits[0]["note_id"] is None


def test_list_notes_surfaces_index_flag(store: NotesStore, repo: Path) -> None:
    (repo / "canon.md").write_text("---\nindex: true\n---\n\n# Canon\n")
    (repo / "scratch.md").write_text("# Scratch\n")
    notes = {n["id"]: n["indexed"] for n in store.list_notes()["notes"]}
    assert notes["canon.md"] is True
    assert notes["scratch.md"] is False


def test_read_note_file_ranged(store: NotesStore, repo: Path) -> None:
    from notes_mcp.files import read_note_file

    big = "\n".join(f"row {i}" for i in range(1, 301))
    (repo / "cycling-analysis" / "data.txt").write_text(big)
    window = read_note_file(store, "cycling-analysis", "data.txt", start_line=100, limit=10)
    assert window["content"].splitlines() == [f"row {i}" for i in range(100, 110)]
    assert window["total_lines"] == 300
    whole = read_note_file(store, "cycling-analysis", "data.txt")
    assert len(whole["content"].splitlines()) == 300
