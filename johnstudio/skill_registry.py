"""CRUD over the normalized skill registry. Backed by SQLite + on-disk YAML."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from . import config, db, utils
from .models import SkillMetadata


def _meta_path(skill_id: str) -> Path:
    return config.home_dir() / "skill-registry" / "skills" / skill_id / "metadata.yaml"


def load_meta(skill_id: str) -> SkillMetadata | None:
    p = _meta_path(skill_id)
    if not p.exists():
        return None
    return SkillMetadata.model_validate(yaml.safe_load(p.read_text()))


def save_meta(meta: SkillMetadata) -> None:
    utils.write_yaml(_meta_path(meta.id), meta.model_dump(mode="json"))
    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        """UPDATE skills SET enabled = ?, trust_level = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP
           WHERE skill_id = ?""",
        (1 if meta.enabled else 0, meta.trust_level,
         json.dumps(meta.model_dump(mode="json")), meta.id),
    )
    conn.commit()
    conn.close()


def list_skills(
    *, enabled_only: bool = False, category: str | None = None
) -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    sql = "SELECT skill_id, type, name, description, category, enabled, trust_level FROM skills"
    where = []
    args: list = []
    if enabled_only:
        where.append("enabled = 1")
    if category:
        where.append("category = ?")
        args.append(category)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, skill_id"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    return rows


def show_skill(skill_id: str) -> dict | None:
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute(
        "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


def search_skills(query: str) -> list[dict]:
    """Substring search across name/description/category/tags. LIKE fallback (FTS5 optional)."""
    q = f"%{query.lower()}%"
    conn = db.connect()
    db.init_schema(conn)
    rows = conn.execute(
        """SELECT skill_id, name, description, category FROM skills
           WHERE LOWER(name) LIKE ? OR LOWER(description) LIKE ?
              OR LOWER(category) LIKE ? OR LOWER(IFNULL(tags_json, '')) LIKE ?
           ORDER BY skill_id""",
        (q, q, q, q),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_enabled(skill_id: str, enabled: bool) -> None:
    meta = load_meta(skill_id)
    if not meta:
        raise KeyError(f"Skill not found: {skill_id}")
    meta.enabled = enabled
    save_meta(meta)


def set_trust(skill_id: str, trust_level: str) -> None:
    meta = load_meta(skill_id)
    if not meta:
        raise KeyError(f"Skill not found: {skill_id}")
    meta.trust_level = trust_level  # type: ignore[assignment]
    save_meta(meta)


# ---------------------------------------------------------------------------
# Pinning (project-level)
# ---------------------------------------------------------------------------

def pin_skill(repo_path: str | Path, skill_id: str) -> list[str]:
    cfg = config.load_project_config(repo_path)
    if skill_id not in cfg.pinned_skills:
        cfg.pinned_skills.append(skill_id)
        config.write_project_config(cfg)
    return cfg.pinned_skills


def unpin_skill(repo_path: str | Path, skill_id: str) -> list[str]:
    cfg = config.load_project_config(repo_path)
    cfg.pinned_skills = [s for s in cfg.pinned_skills if s != skill_id]
    config.write_project_config(cfg)
    return cfg.pinned_skills
