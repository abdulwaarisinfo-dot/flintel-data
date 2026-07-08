"""
db.py — zero-dependency persistence for Flintel (SQLite via stdlib).

Why SQLite: the app has to "just run" on a fresh box with no external
services to provision. One file, WAL mode, safe for the concurrency a
single-node API sees. When you outgrow it, the swap to Postgres is a
matter of changing these helpers — nothing above this layer knows the
storage engine.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

DB_PATH = os.getenv("FLINTEL_DB", os.path.join(os.path.dirname(__file__), "flintel.db"))

# One connection, guarded by a lock. SQLite handles our write volume fine;
# the lock just serializes writes so we never trip "database is locked".
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    """Create tables if they don't exist. Idempotent — safe on every boot."""
    global _conn
    _conn = _connect()
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id            TEXT PRIMARY KEY,
            name          TEXT,
            email         TEXT UNIQUE,
            password_hash TEXT,
            company       TEXT,
            plan          TEXT,
            created_at    REAL
        );

        CREATE TABLE IF NOT EXISTS tokens (
            token      TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            created_at REAL,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS scans (
            id          TEXT PRIMARY KEY,
            account_id  TEXT,
            domain      TEXT,
            status      TEXT,            -- processing | ready | error
            report_json TEXT,
            error       TEXT,
            created_at  REAL,
            updated_at  REAL
        );

        CREATE TABLE IF NOT EXISTS onboarding (
            account_id TEXT PRIMARY KEY,
            profile    TEXT,
            audience   TEXT,
            subreddits TEXT,
            guardrails TEXT,
            updated_at REAL,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS placements (
            id            TEXT PRIMARY KEY,
            account_id    TEXT,
            subreddit     TEXT,
            thread_title  TEXT,
            thread_url    TEXT,
            draft_comment TEXT,
            status        TEXT,          -- draft | approved | posted | skipped
            scheduled_for REAL,
            created_at    REAL,
            updated_at    REAL,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );
        """
    )
    _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn  # type: ignore[return-value]


# ── accounts ──────────────────────────────────────────────────────────
def create_account(id: str, name: str, email: str, password_hash: str,
                   company: str, plan: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO accounts (id,name,email,password_hash,company,plan,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (id, name, email, password_hash, company, plan, time.time()),
        )
        _c().commit()


def get_account_by_email(email: str) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()


def get_account(account_id: str) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


# ── tokens ────────────────────────────────────────────────────────────
def create_token(token: str, account_id: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO tokens (token,account_id,created_at) VALUES (?,?,?)",
            (token, account_id, time.time()),
        )
        _c().commit()


def account_id_for_token(token: str) -> Optional[str]:
    with _lock:
        row = _c().execute("SELECT account_id FROM tokens WHERE token=?", (token,)).fetchone()
    return row["account_id"] if row else None


# ── scans ─────────────────────────────────────────────────────────────
def create_scan(id: str, account_id: Optional[str], domain: str) -> None:
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO scans (id,account_id,domain,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (id, account_id, domain, "processing", now, now),
        )
        _c().commit()


def finish_scan(id: str, report: dict[str, Any]) -> None:
    with _lock:
        _c().execute(
            "UPDATE scans SET status='ready', report_json=?, updated_at=? WHERE id=?",
            (json.dumps(report), time.time(), id),
        )
        _c().commit()


def fail_scan(id: str, error: str) -> None:
    with _lock:
        _c().execute(
            "UPDATE scans SET status='error', error=?, updated_at=? WHERE id=?",
            (error, time.time(), id),
        )
        _c().commit()


def get_scan(id: str) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute("SELECT * FROM scans WHERE id=?", (id,)).fetchone()


# ── onboarding ────────────────────────────────────────────────────────
def save_onboarding(account_id: str, profile: str, audience: str,
                    subreddits: str, guardrails: str) -> None:
    with _lock:
        _c().execute(
            "INSERT INTO onboarding (account_id,profile,audience,subreddits,guardrails,updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(account_id) DO UPDATE SET"
            "   profile=excluded.profile, audience=excluded.audience,"
            "   subreddits=excluded.subreddits, guardrails=excluded.guardrails,"
            "   updated_at=excluded.updated_at",
            (account_id, profile, audience, subreddits, guardrails, time.time()),
        )
        _c().commit()


def get_onboarding(account_id: str) -> Optional[sqlite3.Row]:
    with _lock:
        return _c().execute(
            "SELECT * FROM onboarding WHERE account_id=?", (account_id,)
        ).fetchone()


# ── placements ────────────────────────────────────────────────────────
def create_placement(id: str, account_id: str, subreddit: str, thread_title: str,
                     thread_url: str, draft_comment: str) -> None:
    now = time.time()
    with _lock:
        _c().execute(
            "INSERT INTO placements (id,account_id,subreddit,thread_title,thread_url,"
            "draft_comment,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (id, account_id, subreddit, thread_title, thread_url, draft_comment,
             "draft", now, now),
        )
        _c().commit()


def list_placements(account_id: str) -> list[sqlite3.Row]:
    with _lock:
        return _c().execute(
            "SELECT * FROM placements WHERE account_id=? ORDER BY created_at DESC",
            (account_id,),
        ).fetchall()


def approve_placement(id: str, scheduled_for: float) -> bool:
    with _lock:
        cur = _c().execute(
            "UPDATE placements SET status='approved', scheduled_for=?, updated_at=?"
            " WHERE id=?",
            (scheduled_for, time.time(), id),
        )
        _c().commit()
        return cur.rowcount > 0
