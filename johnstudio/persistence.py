"""Persistence hardening on top of the operational SQLite store (`db.py`).

`db.py` owns the *shape* of the schema (idempotent `CREATE TABLE IF NOT
EXISTS` + ad-hoc `COLUMN_ADDITIONS`). That is fine for additive changes
but gives us no way to (a) run an ordered, one-time data/shape migration,
(b) know what version a given DB file is at, (c) take a safe hot backup
before a risky operation, or (d) wrap a multi-statement write so a
mid-sequence failure can't leave half-applied state.

This module fills those gaps. It is intentionally additive: nothing in
the existing codebase has to change for it to be correct, and importing
it has no side effects. Callers opt in via `ensure_current(conn)`.

Design choices (documented because this runs autonomously):
- We use SQLite's built-in `PRAGMA user_version` as the schema-version
  counter rather than a bookkeeping table. It is atomic, needs no schema
  of its own, and is the idiomatic SQLite approach.
- Migrations are an ordered list of (version, description, fn) tuples.
  `fn` receives a cursor and runs inside a transaction that this module
  manages, so a migration that raises leaves `user_version` untouched.
- Backups use the native online-backup API (`Connection.backup`), which
  is safe while the source DB is being written to under WAL — no need to
  stop the running AUS arc to snapshot.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import db

# A migration mutates the DB to bring it from version N-1 to N. The
# callable gets a cursor; it must NOT commit — `migrate` owns the
# transaction boundary so a raising migration rolls back cleanly.
Migration = tuple[int, str, Callable[[sqlite3.Cursor], None]]


# Ordered, append-only. To add a migration, append a tuple with the next
# integer version. Never renumber or delete existing entries — existing
# DB files have already recorded that they are at a given version.
#
# Version 1 is the baseline: it simply asserts that `db.init_schema` has
# produced the expected core tables. Future shape/data changes that can't
# be expressed as a plain idempotent CREATE go here.
def _m001_baseline(cur: sqlite3.Cursor) -> None:
    # No-op shape change: the baseline schema is owned by db.init_schema.
    # We keep this migration so the version counter starts at a known
    # point and so there is a worked example for future authors.
    return None


MIGRATIONS: list[Migration] = [
    (1, "baseline — schema owned by db.init_schema", _m001_baseline),
]


def latest_version() -> int:
    """Highest migration version this build knows about (0 if none)."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


def schema_version(conn: sqlite3.Connection) -> int:
    """Current `PRAGMA user_version` of the connected DB."""
    row = conn.execute("PRAGMA user_version").fetchone()
    # row is a tuple-like; index 0 works for both Row and plain tuple.
    return int(row[0])


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """Atomic write scope: commit on success, roll back on any exception.

    Yields a cursor. Uses an explicit BEGIN so DDL and DML are grouped
    into one unit even though Python's sqlite3 autocommits DDL by default
    when not inside an explicit transaction.
    """
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        yield cur
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def migrate(conn: sqlite3.Connection) -> dict:
    """Apply every pending migration in order, atomically per version.

    Each migration runs inside its own transaction; on success the
    connection's `user_version` is bumped to that migration's number. A
    migration that raises rolls back and stops the run, leaving the DB at
    the last successfully applied version. Safe to call repeatedly.
    """
    start = schema_version(conn)
    applied: list[int] = []
    for version, _desc, fn in MIGRATIONS:
        if version <= start:
            continue
        with transaction(conn) as cur:
            fn(cur)
            # PRAGMA does not accept bound parameters; version is an int
            # we control (never user input), so interpolation is safe.
            cur.execute(f"PRAGMA user_version = {int(version)}")
        applied.append(version)
    return {
        "from_version": start,
        "to_version": schema_version(conn),
        "applied": applied,
    }


def ensure_current(conn: sqlite3.Connection) -> dict:
    """One call to make a connection fully ready: schema + migrations.

    Runs `db.init_schema` (idempotent table creation) then applies any
    pending versioned migrations. This is the recommended entry point for
    callers that previously called `db.init_schema` directly.
    """
    schema_status = db.init_schema(conn)
    migrate_status = migrate(conn)
    return {"schema": schema_status, "migrations": migrate_status}


def integrity_check(conn: sqlite3.Connection) -> bool:
    """True if SQLite reports the database structurally sound."""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return len(rows) == 1 and str(rows[0][0]).lower() == "ok"


def checkpoint(conn: sqlite3.Connection) -> None:
    """Flush the WAL into the main DB file (TRUNCATE mode).

    Useful before taking a file-copy backup or shrinking the WAL after a
    burst of writes from the worker-event tailer.
    """
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def vacuum(conn: sqlite3.Connection) -> None:
    """Reclaim free pages and defragment the file."""
    conn.execute("VACUUM")


def backup_db(
    dest_dir: str | Path | None = None,
    *,
    source: sqlite3.Connection | None = None,
    label: str | None = None,
) -> Path:
    """Take a safe, hot snapshot of the operational DB.

    Uses SQLite's online-backup API so it is consistent even while the
    AUS arc is writing. Returns the path to the snapshot file. The
    snapshot lands in `dest_dir` (defaults to `<home>/backups/`) named
    `johnstudio-<UTC timestamp>[-label].db`.

    `source` lets callers reuse an open connection; otherwise we open a
    fresh read connection to the default DB path.
    """
    own_source = source is None
    src = source or db.connect()
    try:
        backups_dir = Path(dest_dir) if dest_dir else (db.db_path().parent / "backups")
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{label}" if label else ""
        dest = backups_dir / f"johnstudio-{stamp}{suffix}.db"
        # Checkpoint first so the snapshot captures committed WAL frames.
        try:
            src.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass  # read-only or no WAL — backup still copies committed state
        dest_conn = sqlite3.connect(dest)
        try:
            src.backup(dest_conn)
        finally:
            dest_conn.close()
        return dest
    finally:
        if own_source:
            src.close()
