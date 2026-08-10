from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    userid TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    userid TEXT NOT NULL,
    appli INTEGER NOT NULL,
    startdate INTEGER NOT NULL,
    enddate INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    last_received_at INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    processed_at INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    completed_at INTEGER,
    cancelled_at INTEGER,
    timeout_notified_at INTEGER,
    created_by_user_id INTEGER,
    publish_status TEXT NOT NULL DEFAULT 'none',
    telegram_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    userid TEXT NOT NULL,
    grpid TEXT,
    measured_at INTEGER NOT NULL,
    created_at_withings INTEGER,
    modified_at_withings INTEGER,
    systolic REAL,
    diastolic REAL,
    pulse REAL,
    device_id TEXT,
    model_id INTEGER,
    model TEXT,
    raw_json TEXT NOT NULL,
    first_received_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    webhook_received_at INTEGER,
    source TEXT NOT NULL,
    processed_at INTEGER,
    processing_result TEXT,
    session_id TEXT REFERENCES sessions(id),
    arm TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS measurements_user_grpid
ON measurements(userid, grpid) WHERE grpid IS NOT NULL;
CREATE INDEX IF NOT EXISTS measurements_time ON measurements(measured_at);
CREATE INDEX IF NOT EXISTS measurements_session ON measurements(session_id, measured_at);
CREATE INDEX IF NOT EXISTS sessions_active ON sessions(status, mode, started_at);
CREATE INDEX IF NOT EXISTS webhook_pending ON webhook_events(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    session_id TEXT REFERENCES sessions(id),
    kind TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    telegram_message_id INTEGER
);

CREATE INDEX IF NOT EXISTS telegram_outbox_pending
ON telegram_outbox(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            connection = self._connect()
            try:
                connection.executescript(SCHEMA)
                connection.execute(
                    "UPDATE webhook_events SET status = 'pending' WHERE status = 'processing'"
                )
                # A send interrupted after Telegram accepted it cannot be retried safely.
                connection.execute(
                    "UPDATE telegram_outbox SET status = 'uncertain', "
                    "last_error = 'service restarted during send' WHERE status = 'sending'"
                )
                connection.commit()
            finally:
                connection.close()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[sqlite3.Connection]:
        async with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[sqlite3.Connection]:
        async with self._lock:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()
