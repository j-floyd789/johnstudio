"""SQLite operational store. Stdlib sqlite3 only.

Schema is idempotent (CREATE TABLE IF NOT EXISTS). FTS5 is used when the
runtime supports it; otherwise we fall back to LIKE-based search.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import home_dir

DB_FILENAME = "johnstudio.db"

SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        repo_path TEXT NOT NULL,
        base_branch TEXT NOT NULL DEFAULT 'main',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        task_number INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        base_branch TEXT NOT NULL DEFAULT 'main',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(project_id, task_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        role TEXT NOT NULL,
        command TEXT NOT NULL,
        can_edit INTEGER NOT NULL DEFAULT 0,
        worktree_enabled INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        worker_id INTEGER NOT NULL REFERENCES workers(id),
        status TEXT NOT NULL DEFAULT 'pending',
        tmux_session TEXT,
        tmux_pane TEXT,
        worktree_path TEXT,
        branch_name TEXT,
        prompt_path TEXT,
        result_path TEXT,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        from_worker TEXT,
        to_worker TEXT,
        kind TEXT,
        content TEXT,
        path TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_url TEXT,
        local_path TEXT,
        status TEXT NOT NULL DEFAULT 'registered',
        last_scanned_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER REFERENCES skill_sources(id),
        skill_id TEXT NOT NULL UNIQUE,
        type TEXT NOT NULL DEFAULT 'skill',
        name TEXT NOT NULL,
        description TEXT,
        category TEXT,
        tags_json TEXT,
        metadata_json TEXT,
        original_path TEXT,
        distilled_path TEXT,
        summary_path TEXT,
        enabled INTEGER NOT NULL DEFAULT 0,
        trust_level TEXT NOT NULL DEFAULT 'unreviewed',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER REFERENCES tasks(id),
        skill_id TEXT,
        worker_id INTEGER REFERENCES workers(id),
        usefulness INTEGER,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        worker_id INTEGER REFERENCES workers(id),
        command TEXT NOT NULL,
        exit_code INTEGER,
        output_path TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        worker_id INTEGER REFERENCES workers(id),
        diff_path TEXT,
        files_changed_json TEXT,
        stats_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        reviewer_worker_id INTEGER REFERENCES workers(id),
        review_path TEXT,
        recommendation TEXT,
        score_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        task_id INTEGER REFERENCES tasks(id),
        title TEXT NOT NULL,
        content_path TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        entity_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT,
        tags_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(project_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chain_meta (
        task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        assignments_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_phases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        phase TEXT NOT NULL,
        round INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        artifact_path TEXT,
        verdict TEXT,
        notes TEXT,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS graph_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        from_entity_id TEXT NOT NULL,
        to_entity_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        source_note_path TEXT,
        confidence REAL NOT NULL DEFAULT 1.0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Per-worker event stream. Populated by a tailer that parses each
    # worker's stream-json sidecar log (one JSON line per Claude turn /
    # tool call). The graph UI subscribes via SSE and renders the latest
    # `summary` as the node's "current step" text.
    """
    CREATE TABLE IF NOT EXISTS worker_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
        task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
        phase_id INTEGER REFERENCES task_phases(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        summary TEXT,
        raw_json TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_worker_events_run ON worker_events(run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_worker_events_task ON worker_events(task_id, id)",
    # Hot-path indexes called out by the deep review.
    "CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_phases_task ON task_phases(task_id, id)",
    # Artifact manifest store. Agents register JSON/file outputs by
    # {kind, path, sha256, tags}; downstream agents refer by ID instead
    # of polling for filenames. sha256 UNIQUE per project dedupes
    # re-registrations.
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL,
        task_number INTEGER,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER,
        agent TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(project_id, sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_tags (
        artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY(artifact_id, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_artifact_tags_tag ON artifact_tags(tag)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_project_kind ON artifacts(project_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(project_id, task_number)",
    # Per-task shared-state blackboard. Agents post TTL'd hints other
    # agents read (via Blackboard.snapshot, injected into prompts, and
    # via MCP tools). Rows are deleted by a GC daemon once expired; live
    # reads filter expires_at > NOW() so expired entries are invisible
    # even before the sweep.
    """
    CREATE TABLE IF NOT EXISTS blackboard (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL,
        task_number INTEGER NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        agent TEXT,
        posted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TEXT NOT NULL,
        UNIQUE(project_id, task_number, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bb_lookup ON blackboard(project_id, task_number, expires_at)",
    # Confidence-scored arc-iter / lesson patterns. Mirrored into the
    # vector store under namespace 'patterns' for semantic retrieval at
    # planning time. See johnstudio.patterns.
    """
    CREATE TABLE IF NOT EXISTS patterns (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.7,
        tags_json TEXT NOT NULL DEFAULT '[]',
        evidence_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
        source_task_number INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_patterns_project_kind ON patterns(project_id, kind)",
]


# Schema additions that need ALTER TABLE on existing DBs. CREATE TABLE
# IF NOT EXISTS doesn't add columns. Each entry is a (table, column,
# decl) tuple; init_schema applies them idempotently via PRAGMA
# table_info inspection.
COLUMN_ADDITIONS: list[tuple[str, str, str]] = [
    # PID of the worker subprocess so we can find/kill it after backend
    # restart (orphans were burning tokens with no way to track them).
    ("runs", "pid", "INTEGER"),
    # Per-run notional cost from Claude's `total_cost_usd` event field,
    # accumulated across turns. Lets us enforce a per-task budget.
    ("runs", "cost_usd", "REAL NOT NULL DEFAULT 0.0"),
    # Optional per-task hard budget. When set, the orchestrator stops
    # spawning new workers and flags the task as `budget_exceeded` once
    # the rolling sum of run cost crosses this.
    ("tasks", "budget_usd", "REAL"),
    # Rolling per-task cost cache (sum of constituent runs). Updated by
    # the tailer's cost-update path so the kill-switch check is a
    # single-row read instead of a SUM aggregate per insert.
    ("tasks", "cost_usd", "REAL NOT NULL DEFAULT 0.0"),
    # Final exit code of the worker process. Populated when the run
    # finishes (or we detect a 'result' event). Lets us distinguish
    # "claude crashed" from "claude succeeded but wrote nothing."
    ("runs", "exit_code", "INTEGER"),
]

FTS_STATEMENTS: list[str] = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
        skill_id UNINDEXED, name, description, category, tags, body
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        project_id UNINDEXED, path UNINDEXED, title, body, tags
    )
    """,
]


def db_path() -> Path:
    return home_dir() / DB_FILENAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so the worker_events tailer threads can
    # share the same DB file (each opens its own connection — the flag
    # just disables sqlite3's per-connection thread-affinity assertion).
    conn = sqlite3.connect(p, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL gives us concurrent readers + a single writer without the
    # default rollback-journal "database is locked" thrash under load.
    # busy_timeout lets the writer wait briefly when contended instead
    # of failing immediately.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")  # WAL + NORMAL = durable enough, much faster
    return conn


_FTS5_CACHE: bool | None = None


def has_fts5(conn: sqlite3.Connection) -> bool:
    # FTS5 availability is a property of the sqlite build, not the DB — constant
    # within a process. Probing with CREATE+DROP DDL on EVERY connect (init_schema
    # runs on ~every request + 3 daemon threads) took the write lock and serialized
    # readers against writers. Probe once, then cache.
    global _FTS5_CACHE
    if _FTS5_CACHE is not None:
        return _FTS5_CACHE
    try:
        conn.execute("CREATE VIRTUAL TABLE __probe USING fts5(x)")
        conn.execute("DROP TABLE __probe")
        _FTS5_CACHE = True
    except sqlite3.OperationalError:
        _FTS5_CACHE = False
    return _FTS5_CACHE


def init_schema(conn: sqlite3.Connection) -> dict:
    """Create all tables + apply column additions idempotently.

    Returns a small status dict. Safe to call repeatedly.
    """
    cur = conn.cursor()
    for stmt in SCHEMA_STATEMENTS:
        cur.execute(stmt)
    # Apply ALTER-style column additions. SQLite has no
    # "ADD COLUMN IF NOT EXISTS"; we introspect PRAGMA table_info.
    for table, col, decl in COLUMN_ADDITIONS:
        existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    fts = has_fts5(conn)
    if fts:
        for stmt in FTS_STATEMENTS:
            cur.execute(stmt)
    conn.commit()
    return {"fts5": fts, "tables": len(SCHEMA_STATEMENTS), "alters": len(COLUMN_ADDITIONS)}
