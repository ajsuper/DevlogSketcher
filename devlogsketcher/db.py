"""SQLite persistence for the post database (one store.db per project).

The post database is the heart of the tool: future runs read it to see what's
already been suggested, researched, published, or rejected, so ideas don't repeat
and finished work drops out of the candidate pool.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# Entry lifecycle. Future runs keep published/rejected out of the candidate pool
# and flag `stale` (superseded by later changes) for refresh.
STATUSES = (
    "suggested",    # planner proposed it
    "researched",   # research agent deepened the outline
    "in_progress",  # user is writing it
    "published",    # done
    "rejected",     # user declined it
    "stale",        # superseded by later changes; candidate for refresh
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Entry:
    id: int
    created_at: str
    updated_at: str
    audience: str
    status: str
    title: str
    summary: str
    outline: str
    source_refs: list[str]
    run_id: int | None


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS run (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                window_days INTEGER,
                since       TEXT,
                num_commits INTEGER,
                notes       TEXT
            );
            CREATE TABLE IF NOT EXISTS post_entry (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                audience    TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'suggested',
                title       TEXT NOT NULL,
                summary     TEXT NOT NULL DEFAULT '',
                outline     TEXT NOT NULL DEFAULT '',
                source_refs TEXT NOT NULL DEFAULT '[]',
                run_id      INTEGER REFERENCES run(id)
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- runs -------------------------------------------------------------

    def create_run(self, window_days: int, since: str, num_commits: int, notes: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO run (created_at, window_days, since, num_commits, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), window_days, since, num_commits, notes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # --- entries ----------------------------------------------------------

    def add_entry(
        self,
        audience: str,
        title: str,
        summary: str = "",
        outline: str = "",
        source_refs: list[str] | None = None,
        status: str = "suggested",
        run_id: int | None = None,
    ) -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO post_entry "
            "(created_at, updated_at, audience, status, title, summary, outline, source_refs, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, audience, status, title, summary, outline,
             json.dumps(source_refs or []), run_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_entry(self, entry_id: int, **fields) -> None:
        if not fields:
            return
        if "source_refs" in fields and isinstance(fields["source_refs"], list):
            fields["source_refs"] = json.dumps(fields["source_refs"])
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE post_entry SET {cols} WHERE id = ?",
            (*fields.values(), entry_id),
        )
        self.conn.commit()

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self.conn.execute(
            "SELECT * FROM post_entry WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def list_entries(
        self, status: str | None = None, audience: str | None = None
    ) -> list[Entry]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if audience:
            clauses.append("audience = ?")
            params.append(audience)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM post_entry {where} ORDER BY updated_at DESC", params
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> Entry:
        d = dict(row)
        d["source_refs"] = json.loads(d["source_refs"])
        return Entry(**d)
