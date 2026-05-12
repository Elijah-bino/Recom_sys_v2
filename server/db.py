from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Db:
    """Thin SQLite wrapper (single-file DB)."""

    path: Path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS liked_imdb (
            user_id INTEGER NOT NULL,
            imdb_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, imdb_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rec_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mode TEXT NOT NULL,
            query TEXT,
            filters_json TEXT,
            results_json TEXT NOT NULL,
            share_slug TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_rec_runs_user ON rec_runs(user_id);
        """
    )
    conn.commit()


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE LIMIT 1", (email.strip(),))
    return cur.fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM users WHERE id = ? LIMIT 1", (int(user_id),))
    return cur.fetchone()


def create_user(conn: sqlite3.Connection, *, email: str, password_hash: str, created_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        (email.strip(), password_hash, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_liked_imdb(conn: sqlite3.Connection, user_id: int) -> list[str]:
    cur = conn.execute(
        "SELECT imdb_id FROM liked_imdb WHERE user_id = ? ORDER BY created_at DESC",
        (int(user_id),),
    )
    return [str(r[0]) for r in cur.fetchall()]


def set_liked(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> None:
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO liked_imdb (user_id, imdb_id, created_at) VALUES (?, ?, ?)",
        (int(user_id), imdb_id.strip(), ts),
    )
    conn.commit()


def unset_liked(conn: sqlite3.Connection, user_id: int, imdb_id: str) -> None:
    conn.execute("DELETE FROM liked_imdb WHERE user_id = ? AND imdb_id = ?", (int(user_id), imdb_id.strip()))
    conn.commit()


def insert_rec_run(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    mode: str,
    query: str | None,
    filters: dict[str, Any] | None,
    results: list[dict[str, Any]],
    share_slug: str,
    created_at: str,
) -> int:
    conn.execute(
        """
        INSERT INTO rec_runs (user_id, mode, query, filters_json, results_json, share_slug, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            mode,
            query,
            json.dumps(filters) if filters is not None else None,
            json.dumps(results),
            share_slug,
            created_at,
        ),
    )
    conn.commit()
    cur = conn.execute("SELECT last_insert_rowid() AS id")
    return int(cur.fetchone()[0])


def fetch_run_by_slug(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM rec_runs WHERE share_slug = ? LIMIT 1", (slug.strip(),))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["results"] = json.loads(d["results_json"])
    except Exception:
        d["results"] = []
    return d


def list_recent_runs(conn: sqlite3.Connection, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, mode, query, share_slug, created_at
        FROM rec_runs
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(user_id), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]
