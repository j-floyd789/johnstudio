from __future__ import annotations

from pathlib import Path

from johnstudio import knowledge_graph as kg, memory, project as project_mod


def test_create_entity_writes_note_and_db(jh_home, git_repo):
    pid = project_mod.add_project("demo", git_repo)["project_id"]
    p = kg.create_entity(
        project_id=pid,
        repo_path=git_repo,
        entity_type="concept",
        name="Stripe Billing",
        tags=["billing"],
        metadata={"status": "active"},
    )
    assert p.exists()
    text = p.read_text()
    assert "id: concept-stripe-billing" in text
    assert "type: concept" in text
    assert "billing" in text
    rows = kg.list_entities(pid)
    assert any(r["entity_id"] == "concept-stripe-billing" for r in rows)


def test_link_entities(jh_home, git_repo):
    pid = project_mod.add_project("demo", git_repo)["project_id"]
    kg.create_entity(pid, git_repo, "concept", "Stripe Billing")
    kg.create_entity(pid, git_repo, "system", "Auth System")
    kg.link_entities(
        pid,
        ("concept", "Stripe Billing"),
        ("system", "Auth System"),
        "depends_on",
    )
    rels = kg.list_relationships(pid)
    assert len(rels) == 1
    assert rels[0]["relation_type"] == "depends_on"


def test_extract_entities_deterministic():
    text = "We use Stripe and Postgres with Next.js. See [[Project - Foo]] in task-0007 #billing"
    out = kg.extract_entities_deterministic(text)
    assert "stripe" in out["technologies"]
    assert "postgres" in out["technologies"]
    assert "nextjs" in out["technologies"]
    assert "Project - Foo" in out["wiki_links"]
    assert "task-0007" in out["tasks"]
    assert "billing" in out["tags"]


def test_auto_tag_note_preserves_frontmatter(tmp_path):
    note = tmp_path / "n.md"
    note.write_text(
        "---\nname: Foo\ntags: [existing]\n---\n\n# Foo\n\nUses Stripe and Postgres.\n"
    )
    added = kg.auto_tag_note(note)
    assert "stripe" in added and "postgres" in added
    text = note.read_text()
    assert "existing" in text and "stripe" in text


def test_build_backlink_index(tmp_path):
    root = memory.init_vault(tmp_path)
    (root / "a.md").write_text("Links to [[B Page]] and [[C Page]]\n")
    (root / "b.md").write_text("Backlinks [[C Page]]\n")
    idx = kg.build_backlink_index(tmp_path)
    assert "B Page" in idx and "C Page" in idx
    assert len(idx["C Page"]) == 2


def test_auto_link_note_appends_links(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("# Body\n\nThis mentions Stripe Billing and Auth System.\n")
    added = kg.auto_link_note(note, known_entities=["Stripe Billing", "Auth System", "Unrelated"])
    assert added == ["Auth System", "Stripe Billing"]
    text = note.read_text()
    assert "[[Stripe Billing]]" in text and "[[Auth System]]" in text
    assert "Unrelated" not in text
