"""Obsidian-compatible knowledge-graph memory.

Each entity is a markdown note with YAML frontmatter inside a typed subfolder
under `<repo>/.johnstudio/memory/graph/<type>/`. Relationships are stored in
SQLite (`graph_relationships`) AND surface as `[[wiki links]]` inside notes
so the graph is browsable directly in Obsidian.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import db, memory, utils

# ---------------------------------------------------------------------------
# Entity types -> graph folder name
# ---------------------------------------------------------------------------

TYPE_TO_FOLDER: dict[str, str] = {
    "person": "people",
    "project": "projects",
    "task": "tasks",
    "agent": "agents",
    "system": "systems",
    "architecture": "systems",
    "concept": "concepts",
    "decision": "decisions",
    "bug": "bugs",
    "file": "files",
    "feature": "features",
}

TYPE_TO_TITLE_PREFIX: dict[str, str] = {
    "person": "Person",
    "project": "Project",
    "task": "Task",
    "agent": "Agent",
    "system": "Architecture",
    "architecture": "Architecture",
    "concept": "Concept",
    "decision": "Decision",
    "bug": "Bug",
    "file": "File",
    "feature": "Feature",
}


# ---------------------------------------------------------------------------
# Entity I/O
# ---------------------------------------------------------------------------

def _entity_path(repo_path: str | Path, entity_type: str, name: str) -> Path:
    folder = TYPE_TO_FOLDER.get(entity_type, "concepts")
    prefix = TYPE_TO_TITLE_PREFIX.get(entity_type, entity_type.title())
    fname = f"{prefix} - {name}.md"
    return memory.graph_root(repo_path) / folder / fname


def _entity_id(entity_type: str, name: str) -> str:
    return f"{entity_type}-{utils.slugify(name)}"


def create_entity(
    project_id: int,
    repo_path: str | Path,
    entity_type: str,
    name: str,
    *,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    body: str | None = None,
) -> Path:
    """Create (or update) a graph entity markdown note and DB row.

    Returns the note path.
    """
    tags = tags or []
    metadata = metadata or {}
    eid = _entity_id(entity_type, name)
    path = _entity_path(repo_path, entity_type, name)
    path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "id": eid,
        "type": entity_type,
        "name": name,
        "tags": sorted(set(tags + [entity_type])),
        **metadata,
        "created_at": metadata.get("created_at") or datetime.utcnow().date().isoformat(),
    }
    title = f"{TYPE_TO_TITLE_PREFIX.get(entity_type, entity_type.title())} - {name}"
    body_md = body or f"# {title}\n\n_Created by JohnStudio._\n"
    content = utils.join_frontmatter(frontmatter, body_md if body_md.startswith("#") else f"# {title}\n\n{body_md}")
    path.write_text(content, encoding="utf-8")

    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        """INSERT INTO graph_entities (project_id, entity_id, entity_type, name, path, tags_json, metadata_json)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(project_id, entity_id) DO UPDATE SET
               name = excluded.name,
               path = excluded.path,
               tags_json = excluded.tags_json,
               metadata_json = excluded.metadata_json,
               updated_at = CURRENT_TIMESTAMP""",
        (
            project_id, eid, entity_type, name, str(path),
            json.dumps(frontmatter["tags"]),
            json.dumps(metadata),
        ),
    )
    conn.commit()
    conn.close()
    return path


def link_entities(
    project_id: int,
    from_entity: tuple[str, str],
    to_entity: tuple[str, str],
    relation_type: str,
    *,
    source_note_path: str | None = None,
    confidence: float = 1.0,
) -> None:
    """Record a relationship in the graph DB."""
    f_id = _entity_id(*from_entity)
    t_id = _entity_id(*to_entity)
    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        """INSERT INTO graph_relationships
           (project_id, from_entity_id, to_entity_id, relation_type, source_note_path, confidence)
           VALUES (?,?,?,?,?,?)""",
        (project_id, f_id, t_id, relation_type, source_note_path, confidence),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Deterministic entity extraction & tagging
# ---------------------------------------------------------------------------

KNOWN_TECHS: dict[str, str] = {
    "stripe": "stripe",
    "supabase": "supabase",
    "postgres": "postgres",
    "postgresql": "postgres",
    "prisma": "prisma",
    "next.js": "nextjs",
    "nextjs": "nextjs",
    "react": "react",
    "vite": "vite",
    "tailwind": "tailwind",
    "typescript": "typescript",
    "python": "python",
    "fastapi": "fastapi",
    "django": "django",
    "docker": "docker",
    "kubernetes": "kubernetes",
    "graphql": "graphql",
}

WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])#([a-z][a-z0-9\-]+)")
TASK_ID_RE = re.compile(r"\btask-(\d{4})\b", re.IGNORECASE)


def extract_entities_deterministic(text: str) -> dict[str, list[str]]:
    """Pure extractor. Returns {kind: [name, ...]} dicts of unique values.

    Kinds: 'tags', 'wiki_links', 'tasks', 'technologies'.
    """
    low = text.lower()
    techs = sorted({
        canon for token, canon in KNOWN_TECHS.items()
        if re.search(rf"\b{re.escape(token)}\b", low)
    })
    return {
        "tags": sorted(set(HASHTAG_RE.findall(text.lower()))),
        "wiki_links": sorted({m.strip() for m in WIKI_LINK_RE.findall(text)}),
        "tasks": sorted({f"task-{n.zfill(4)}" for n in TASK_ID_RE.findall(text)}),
        "technologies": techs,
    }


def auto_tag_note(path: Path) -> list[str]:
    """Add detected technology tags to a note's YAML frontmatter.

    Preserves existing frontmatter. Returns the list of tags added.
    """
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    fm, body = utils.split_frontmatter(raw)
    found = extract_entities_deterministic(body)
    existing = set(fm.get("tags", []) or [])
    new = sorted(set(found["technologies"]) - existing)
    if not new:
        return []
    fm["tags"] = sorted(existing | set(new))
    path.write_text(utils.join_frontmatter(fm, body), encoding="utf-8")
    return new


def auto_link_note(path: Path, known_entities: Iterable[str]) -> list[str]:
    """Append a `## Links` section listing wiki-links to known entities found in body.

    Non-destructive: does not modify existing prose; appends a Links section if any new
    matches are found and no `## Links` section already exists.
    """
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    fm, body = utils.split_frontmatter(raw)
    found_names = {name for name in known_entities if name and name.lower() in body.lower()}
    if not found_names:
        return []
    if "## Links" in body:
        return []
    body = body.rstrip() + "\n\n## Links\n" + "\n".join(f"- [[{n}]]" for n in sorted(found_names)) + "\n"
    path.write_text(utils.join_frontmatter(fm, body), encoding="utf-8")
    return sorted(found_names)


def build_backlink_index(repo_path: str | Path) -> dict[str, list[str]]:
    """Return mapping of `wiki-link target -> [notes that link to it]`."""
    root = memory.memory_root(repo_path)
    index: dict[str, list[str]] = {}
    if not root.exists():
        return index
    for p in utils.iter_markdown_files(root):
        text = p.read_text(encoding="utf-8")
        for target in WIKI_LINK_RE.findall(text):
            index.setdefault(target.strip(), []).append(str(p))
    return {k: sorted(set(v)) for k, v in index.items()}


def list_entities(project_id: int) -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT entity_id, entity_type, name, path FROM graph_entities WHERE project_id = ? ORDER BY entity_type, name",
        (project_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def list_relationships(project_id: int) -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "SELECT from_entity_id, to_entity_id, relation_type, confidence FROM graph_relationships WHERE project_id = ?",
        (project_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
