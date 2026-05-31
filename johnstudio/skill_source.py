"""Skill sources: add/scan local or remote skill sources, then `import` them."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import db, skill_importer


def _strip_file_scheme(uri: str) -> str:
    if uri.startswith("file://"):
        return uri[len("file://"):]
    return uri


def add_source(repo_or_path: str) -> dict:
    p = _strip_file_scheme(repo_or_path)
    path = Path(p).expanduser()
    is_local = path.exists() and path.is_dir()
    conn = db.connect()
    db.init_schema(conn)
    cur = conn.execute(
        "INSERT INTO skill_sources (repo_url, local_path, status) VALUES (?,?,?) RETURNING id",
        (None if is_local else repo_or_path, str(path.resolve()) if is_local else None, "registered"),
    )
    sid = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return {"id": sid, "local": is_local, "path": str(path.resolve()) if is_local else None}


def list_sources() -> list[dict]:
    conn = db.connect()
    db.init_schema(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, repo_url, local_path, status, last_scanned_at FROM skill_sources ORDER BY id"
    ).fetchall()]
    conn.close()
    return rows


def scan_sources() -> list[dict]:
    """Import all known local sources. Returns per-source result summary."""
    results: list[dict] = []
    for src in list_sources():
        if not src["local_path"]:
            results.append({"id": src["id"], "skipped": "remote (clone manually then re-add as local path)"})
            continue
        imported = skill_importer.import_dir(
            Path(src["local_path"]),
            source_repo=src.get("repo_url"),
            source_id=src["id"],
        )
        conn = db.connect()
        conn.execute(
            "UPDATE skill_sources SET last_scanned_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(timespec="seconds"), src["id"]),
        )
        conn.commit()
        conn.close()
        results.append({"id": src["id"], "imported": len(imported)})
    return results
