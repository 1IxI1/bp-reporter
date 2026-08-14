from __future__ import annotations

import sqlite3

from app.db import Database


async def test_initialize_adds_reply_markup_to_existing_outbox(tmp_path) -> None:
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE telegram_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                session_id TEXT,
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
            )
            """
        )
        connection.execute(
            """
            INSERT INTO telegram_outbox(
                dedupe_key, kind, chat_id, message_text, created_at
            ) VALUES ('existing', 'owner', '100', 'message', 1)
            """
        )

    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(telegram_outbox)")}
        existing = connection.execute(
            "SELECT message_text, reply_markup FROM telegram_outbox WHERE dedupe_key = 'existing'"
        ).fetchone()
    assert "reply_markup" in columns
    assert existing == ("message", None)
