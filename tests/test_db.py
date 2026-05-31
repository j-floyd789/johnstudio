from __future__ import annotations

from johnstudio import db


def test_init_schema_creates_all_tables(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    conn = db.connect()
    status = db.init_schema(conn)
    assert status["tables"] >= 14

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in cur.fetchall()}
    expected = {
        "projects", "tasks", "workers", "runs", "messages",
        "skill_sources", "skills", "skill_feedback",
        "test_results", "diffs", "reviews", "decisions",
        "graph_entities", "graph_relationships",
    }
    assert expected.issubset(tables)


def test_init_schema_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    conn = db.connect()
    db.init_schema(conn)
    # Insert a row, re-init, row should survive.
    conn.execute(
        "INSERT INTO projects (name, repo_path, base_branch) VALUES (?,?,?)",
        ("p", "/tmp/p", "main"),
    )
    conn.commit()
    db.init_schema(conn)
    cur = conn.execute("SELECT COUNT(*) AS c FROM projects")
    assert cur.fetchone()["c"] == 1


def test_unique_constraints(monkeypatch, tmp_path):
    import sqlite3 as _sqlite3
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    conn = db.connect()
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("p", "/tmp/p")
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("p", "/tmp/q")
        )
        conn.commit()
        raised = False
    except _sqlite3.IntegrityError:
        raised = True
    assert raised


def test_has_fts5_reports_a_bool(monkeypatch, tmp_path):
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    conn = db.connect()
    assert isinstance(db.has_fts5(conn), bool)
