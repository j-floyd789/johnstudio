from __future__ import annotations

import sqlite3

import pytest

from johnstudio import db, persistence


def _conn(monkeypatch, tmp_path) -> sqlite3.Connection:
    monkeypatch.setenv("JOHNSTUDIO_HOME", str(tmp_path))
    return db.connect()


def test_ensure_current_runs_schema_and_migrations(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    status = persistence.ensure_current(conn)
    assert status["schema"]["tables"] >= 14
    assert status["migrations"]["to_version"] == persistence.latest_version()
    assert persistence.schema_version(conn) == persistence.latest_version()


def test_migrate_is_idempotent(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    first = persistence.migrate(conn)
    assert first["applied"] == [m[0] for m in persistence.MIGRATIONS]
    second = persistence.migrate(conn)
    assert second["applied"] == []
    assert second["from_version"] == persistence.latest_version()


def test_transaction_commits_on_success(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    with persistence.transaction(conn) as cur:
        cur.execute(
            "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("p", "/tmp/p")
        )
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 1


def test_transaction_rolls_back_on_error(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with persistence.transaction(conn) as cur:
            cur.execute(
                "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("p", "/tmp/p")
            )
            # Duplicate UNIQUE(name) — forces a failure mid-transaction.
            cur.execute(
                "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("p", "/tmp/q")
            )
    assert conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0


def test_integrity_check_passes_on_fresh_db(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    assert persistence.integrity_check(conn) is True


def test_checkpoint_and_vacuum_do_not_raise(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    persistence.checkpoint(conn)
    persistence.vacuum(conn)


def test_backup_db_creates_consistent_snapshot(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    persistence.ensure_current(conn)
    conn.execute(
        "INSERT INTO projects (name, repo_path) VALUES (?,?)", ("alpha", "/tmp/a")
    )
    conn.commit()

    dest = persistence.backup_db(source=conn, label="test")
    assert dest.exists()

    snap = sqlite3.connect(dest)
    try:
        snap.row_factory = sqlite3.Row
        names = {r["name"] for r in snap.execute("SELECT name FROM projects")}
        assert "alpha" in names
        # Snapshot carries the schema version forward.
        assert int(snap.execute("PRAGMA user_version").fetchone()[0]) == (
            persistence.latest_version()
        )
    finally:
        snap.close()


def test_backup_default_location(monkeypatch, tmp_path):
    conn = _conn(monkeypatch, tmp_path)
    db.init_schema(conn)
    conn.close()
    dest = persistence.backup_db()
    assert dest.exists()
    assert dest.parent == db.db_path().parent / "backups"
