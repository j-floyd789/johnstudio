"""Per-task shared-state blackboard.

Agents working on the same task post short-lived ("TTL'd") hints that
other agents read — rejected candidates, claimed work items, a current
winner, etc. Entries live in the `blackboard` table (see db.py); each is
scoped to a `(project_id, task_number, key)` triple and carries an
`expires_at`. Live reads always filter `expires_at > NOW()` so an expired
entry is invisible even before the GC sweep deletes it.

Surfaces:
  - `Blackboard(project_id, task_number).post/append/get/list/snapshot`
  - `Blackboard.gc()` — classmethod that deletes expired rows globally.

Values are arbitrary JSON-serializable Python objects; they are stored as
JSON in `value_json` and round-tripped on read.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import db

# SQLite stores our timestamps as UTC ISO-8601 strings. We compare against
# strftime('now') in SQL so expiry checks happen in the DB, and we mint our
# own expires_at the same way for consistency.
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> datetime:
    return datetime.utcnow()


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


@dataclass
class BlackboardEntry:
    """A single live blackboard row, with `value_json` decoded."""
    key: str
    value: Any
    agent: str | None
    posted_at: str
    expires_at: str


class Blackboard:
    """Scoped view of the blackboard for one `(project_id, task_number)`."""

    def __init__(self, project_id: int, task_number: int) -> None:
        self.project_id = int(project_id)
        self.task_number = int(task_number)

    # -- writes -------------------------------------------------------------

    def post(
        self,
        *,
        key: str,
        value: Any,
        ttl_seconds: int,
        agent: str | None = None,
    ) -> BlackboardEntry:
        """Upsert a single entry. Replaces any existing live-or-stale row
        with the same key (UNIQUE(project_id, task_number, key))."""
        if ttl_seconds is None or int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        ttl_seconds = int(ttl_seconds)
        now = _now()
        posted_at = _fmt(now)
        expires_at = _fmt(now + timedelta(seconds=ttl_seconds))
        value_json = json.dumps(value)
        conn = db.connect()
        try:
            conn.execute(
                """
                INSERT INTO blackboard
                    (project_id, task_number, key, value_json, agent, posted_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, task_number, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    agent      = excluded.agent,
                    posted_at  = excluded.posted_at,
                    expires_at = excluded.expires_at
                """,
                (
                    self.project_id, self.task_number, key,
                    value_json, agent, posted_at, expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return BlackboardEntry(
            key=key, value=value, agent=agent,
            posted_at=posted_at, expires_at=expires_at,
        )

    def append(
        self,
        *,
        key: str,
        value: Any,
        ttl_seconds: int,
        agent: str | None = None,
    ) -> BlackboardEntry:
        """Append `value` to a list-valued key (read-modify-write).

        If the key is missing (or expired), starts a fresh single-element
        list. If the existing value is not a list it is wrapped into one
        before appending. The TTL is reset on every append.
        """
        existing = self.get(key=key)
        if existing is None:
            new_value: list = [value]
        elif isinstance(existing.value, list):
            new_value = list(existing.value) + [value]
        else:
            # RECONSTRUCTED: behavior when an existing non-list value is
            # appended to is not exercised by tests; we coerce to a list
            # [old, new] rather than raising, which is the least surprising.
            new_value = [existing.value, value]
        return self.post(
            key=key, value=new_value, ttl_seconds=ttl_seconds, agent=agent,
        )

    # -- reads --------------------------------------------------------------

    def get(self, *, key: str) -> BlackboardEntry | None:
        """Return the live entry for `key`, or None if missing/expired."""
        conn = db.connect()
        try:
            row = conn.execute(
                """
                SELECT key, value_json, agent, posted_at, expires_at
                FROM blackboard
                WHERE project_id = ? AND task_number = ? AND key = ?
                  AND expires_at > strftime('%Y-%m-%d %H:%M:%S', 'now')
                """,
                (self.project_id, self.task_number, key),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return _row_to_entry(row)

    def list(self) -> list[dict]:
        """Return all live entries as plain dicts (key/value/agent/...)."""
        conn = db.connect()
        try:
            rows = conn.execute(
                """
                SELECT key, value_json, agent, posted_at, expires_at
                FROM blackboard
                WHERE project_id = ? AND task_number = ?
                  AND expires_at > strftime('%Y-%m-%d %H:%M:%S', 'now')
                ORDER BY posted_at ASC, key ASC
                """,
                (self.project_id, self.task_number),
            ).fetchall()
        finally:
            conn.close()
        out: list[dict] = []
        for row in rows:
            e = _row_to_entry(row)
            out.append({
                "key": e.key,
                "value": e.value,
                "agent": e.agent,
                "posted_at": e.posted_at,
                "expires_at": e.expires_at,
            })
        return out

    def snapshot(self) -> dict[str, Any]:
        """A `{key: value}` map of every live entry for this scope."""
        return {e["key"]: e["value"] for e in self.list()}

    # -- maintenance --------------------------------------------------------

    @classmethod
    def gc(cls) -> int:
        """Delete every expired row across all scopes. Returns the count."""
        conn = db.connect()
        try:
            cur = conn.execute(
                "DELETE FROM blackboard "
                "WHERE expires_at <= strftime('%Y-%m-%d %H:%M:%S', 'now')"
            )
            conn.commit()
            return int(cur.rowcount)
        finally:
            conn.close()


def _row_to_entry(row) -> BlackboardEntry:
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        # RECONSTRUCTED: a corrupt/non-JSON value_json is surfaced raw
        # rather than crashing the reader.
        value = row["value_json"]
    return BlackboardEntry(
        key=row["key"],
        value=value,
        agent=row["agent"],
        posted_at=row["posted_at"],
        expires_at=row["expires_at"],
    )
