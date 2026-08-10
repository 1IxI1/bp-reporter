from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

from app.clients import (
    WITHINGS_AUTHORIZE_URL,
    TelegramAmbiguousError,
    TelegramClient,
    TelegramDefinitiveError,
    WithingsClient,
    WithingsError,
)
from app.config import Settings
from app.db import Database
from app.models import ParsedMeasurement, parse_measure_group

logger = logging.getLogger(__name__)


class BPService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        withings: WithingsClient,
        telegram: TelegramClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.withings = withings
        self.telegram = telegram
        self._sync_lock = asyncio.Lock()

    def now(self) -> int:
        return int(time.time())

    async def create_oauth_url(self) -> str:
        state = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        now = self.now()
        async with self.database.transaction() as connection:
            connection.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
            connection.execute(
                "INSERT INTO oauth_states(state_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (state_hash, now, now + 600),
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.withings_client_id,
                "scope": "user.metrics",
                "redirect_uri": self.settings.withings_redirect_uri,
                "state": state,
            }
        )
        return f"{WITHINGS_AUTHORIZE_URL}?{query}"

    async def consume_oauth_state(self, state: str) -> bool:
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        now = self.now()
        async with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT expires_at, used_at FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if row is None or row["used_at"] is not None or int(row["expires_at"]) < now:
                return False
            connection.execute(
                "UPDATE oauth_states SET used_at = ? WHERE state_hash = ?", (now, state_hash)
            )
            return True

    async def oauth_connected(self) -> bool:
        async with self.database.read() as connection:
            row = connection.execute("SELECT 1 FROM oauth_tokens LIMIT 1").fetchone()
        return row is not None

    async def enqueue_webhook(
        self, *, userid: str, appli: int, startdate: int, enddate: int
    ) -> bool:
        if appli != self.settings.withings_bp_appli:
            raise ValueError("unexpected notification category")
        if startdate > enddate:
            raise ValueError("startdate must not be after enddate")
        if enddate - startdate > 7 * 24 * 60 * 60:
            raise ValueError("notification range is unexpectedly large")
        if self.settings.withings_user_id and userid != self.settings.withings_user_id:
            raise ValueError("unexpected Withings user")
        async with self.database.read() as connection:
            known_user = connection.execute(
                "SELECT 1 FROM oauth_tokens WHERE userid = ?", (userid,)
            ).fetchone()
        if known_user is None:
            raise ValueError("unknown Withings user")

        now = self.now()
        dedupe_key = f"{userid}:{appli}:{startdate}:{enddate}"
        async with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO webhook_events(
                    dedupe_key, userid, appli, startdate, enddate,
                    received_at, last_received_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (dedupe_key, userid, appli, startdate, enddate, now, now, now),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                connection.execute(
                    """
                    UPDATE webhook_events
                    SET duplicate_count = duplicate_count + 1,
                        last_received_at = ?,
                        status = CASE WHEN status = 'failed' THEN 'pending' ELSE status END,
                        next_attempt_at = CASE WHEN status = 'failed' THEN ? ELSE next_attempt_at END
                    WHERE dedupe_key = ?
                    """,
                    (now, now, dedupe_key),
                )
        logger.info("withings_webhook_enqueued", extra={"inserted": inserted})
        return inserted

    async def process_webhook_once(self) -> bool:
        now = self.now()
        async with self.database.transaction() as connection:
            event = connection.execute(
                """
                SELECT * FROM webhook_events
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if event is None:
                return False
            connection.execute(
                "UPDATE webhook_events SET status = 'processing' WHERE id = ?", (event["id"],)
            )
            event_data = dict(event)

        try:
            async with self._sync_lock:
                result = await self.withings.fetch_measurements(
                    str(event_data["userid"]),
                    startdate=max(0, int(event_data["startdate"]) - 5),
                    enddate=int(event_data["enddate"]) + 5,
                )
                stats = await self.ingest_groups(
                    str(event_data["userid"]),
                    result.groups,
                    source="webhook",
                    webhook_received_at=int(event_data["received_at"]),
                )
        except (WithingsError, ValueError, KeyError) as error:
            attempts = int(event_data["attempts"]) + 1
            delay = min(300, 2 ** min(attempts, 8))
            status = "failed" if attempts >= 10 else "pending"
            async with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE webhook_events
                    SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (status, attempts, now + delay, _safe_error(error), event_data["id"]),
                )
            logger.warning(
                "withings_webhook_processing_failed",
                extra={"attempts": attempts, "status_code": getattr(error, "status", None)},
            )
            return True

        async with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE webhook_events
                SET status = 'processed', processed_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (self.now(), event_data["id"]),
            )
        logger.info("withings_webhook_processed", extra=stats)
        return True

    async def poll_once(self) -> dict[str, int]:
        userid = await self.withings.resolve_userid()
        now = self.now()
        cursor_value = await self.get_state("withings_lastupdate")
        lastupdate = (
            int(cursor_value) if cursor_value else now - self.settings.initial_poll_lookback_seconds
        )
        async with self._sync_lock:
            result = await self.withings.fetch_measurements(userid, lastupdate=lastupdate)
            stats = await self.ingest_groups(userid, result.groups, source="poll")

        modified_values = [
            int(group["modified"]) for group in result.groups if group.get("modified") is not None
        ]
        if modified_values:
            await self.set_state("withings_lastupdate", str(max(lastupdate, *modified_values)))
        await self.set_state("poll_last_success", str(self.now()))
        await self.set_state("poll_last_status", "0")
        return stats

    async def ingest_groups(
        self,
        userid: str,
        groups: list[dict[str, Any]],
        *,
        source: str,
        webhook_received_at: int | None = None,
    ) -> dict[str, int]:
        parsed: list[ParsedMeasurement] = []
        invalid = 0
        for group in groups:
            try:
                parsed.append(parse_measure_group(group, userid))
            except (KeyError, TypeError, ValueError):
                invalid += 1
        parsed.sort(key=lambda item: (item.measured_at, item.grpid or ""))

        inserted = 0
        duplicates = 0
        process_ids: list[int] = []
        now = self.now()
        async with self.database.transaction() as connection:
            for item in parsed:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO measurements(
                        dedupe_key, userid, grpid, measured_at, created_at_withings,
                        modified_at_withings, systolic, diastolic, pulse, device_id,
                        model_id, model, raw_json, first_received_at, last_seen_at,
                        webhook_received_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.dedupe_key,
                        item.userid,
                        item.grpid,
                        item.measured_at,
                        item.created_at_withings,
                        item.modified_at_withings,
                        _decimal_float(item.systolic),
                        _decimal_float(item.diastolic),
                        _decimal_float(item.pulse),
                        item.device_id,
                        item.model_id,
                        item.model,
                        item.raw_json,
                        now,
                        now,
                        webhook_received_at,
                        source,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    process_ids.append(int(cursor.lastrowid))
                    continue

                duplicates += 1
                existing = connection.execute(
                    "SELECT id, processed_at, processing_result FROM measurements WHERE dedupe_key = ?",
                    (item.dedupe_key,),
                ).fetchone()
                if existing is None:
                    continue
                can_reprocess = (
                    existing["processed_at"] is None
                    or existing["processing_result"] == "incomplete"
                ) and item.complete
                connection.execute(
                    """
                    UPDATE measurements SET
                        modified_at_withings = ?, systolic = ?, diastolic = ?, pulse = ?,
                        device_id = ?, model_id = ?, model = ?, raw_json = ?,
                        last_seen_at = ?,
                        webhook_received_at = COALESCE(webhook_received_at, ?),
                        source = CASE WHEN source = 'webhook' THEN source ELSE ? END,
                        processed_at = CASE WHEN ? THEN NULL ELSE processed_at END,
                        processing_result = CASE WHEN ? THEN NULL ELSE processing_result END
                    WHERE id = ?
                    """,
                    (
                        item.modified_at_withings,
                        _decimal_float(item.systolic),
                        _decimal_float(item.diastolic),
                        _decimal_float(item.pulse),
                        item.device_id,
                        item.model_id,
                        item.model,
                        item.raw_json,
                        now,
                        webhook_received_at,
                        source,
                        can_reprocess,
                        can_reprocess,
                        existing["id"],
                    ),
                )
                if can_reprocess:
                    process_ids.append(int(existing["id"]))

        for measurement_id in process_ids:
            await self._assign_measurement(measurement_id)
        return {
            "fetched": len(groups),
            "inserted": inserted,
            "duplicates": duplicates,
            "invalid": invalid,
        }

    async def _assign_measurement(self, measurement_id: int) -> None:
        now = self.now()
        live_after = self.settings.live_after_utc
        async with self.database.transaction() as connection:
            measurement = connection.execute(
                "SELECT * FROM measurements WHERE id = ?", (measurement_id,)
            ).fetchone()
            if measurement is None or measurement["processed_at"] is not None:
                return
            if any(measurement[name] is None for name in ("systolic", "diastolic", "pulse")):
                connection.execute(
                    "UPDATE measurements SET processing_result = 'incomplete' WHERE id = ?",
                    (measurement_id,),
                )
                return

            measured_at = int(measurement["measured_at"])
            if live_after is None:
                self._mark_ignored(connection, measurement_id, now, "live_after_not_configured")
                return
            if measured_at <= int(live_after.timestamp()):
                self._mark_ignored(connection, measurement_id, now, "backfill")
                return
            if abs(now - measured_at) > self.settings.measurement_max_age_seconds:
                self._mark_ignored(connection, measurement_id, now, "stale")
                return
            if (
                self.settings.withings_user_id
                and measurement["userid"] != self.settings.withings_user_id
            ):
                self._mark_ignored(connection, measurement_id, now, "unexpected_user")
                return

            self._expire_sessions_in_transaction(connection, now)
            manual = connection.execute(
                """
                SELECT * FROM sessions
                WHERE mode = 'four_arm' AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            if manual is not None:
                if measured_at < int(manual["started_at"]) - 30:
                    self._mark_ignored(connection, measurement_id, now, "before_manual_session")
                    return
                self._assign_to_manual(connection, manual, measurement, now)
                return

            if not self.settings.auto_pair_enabled:
                self._mark_ignored(connection, measurement_id, now, "no_active_session")
                return
            self._assign_to_auto(connection, measurement, now)

    @staticmethod
    def _mark_ignored(
        connection: sqlite3.Connection, measurement_id: int, now: int, reason: str
    ) -> None:
        connection.execute(
            "UPDATE measurements SET processed_at = ?, processing_result = ? WHERE id = ?",
            (now, reason, measurement_id),
        )

    def _assign_to_manual(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        measurement: sqlite3.Row,
        now: int,
    ) -> None:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM measurements WHERE session_id = ?", (session["id"],)
            ).fetchone()[0]
        )
        if count >= 4:
            self._mark_ignored(connection, int(measurement["id"]), now, "session_already_full")
            return
        arm = "left" if count < 2 else "right"
        connection.execute(
            """
            UPDATE measurements
            SET session_id = ?, arm = ?, processed_at = ?, processing_result = 'assigned'
            WHERE id = ?
            """,
            (session["id"], arm, now, measurement["id"]),
        )
        new_count = count + 1
        states = {1: "WAIT_LEFT_2", 2: "WAIT_RIGHT_1", 3: "WAIT_RIGHT_2", 4: "COMPLETE"}
        if new_count < 4:
            connection.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                (states[new_count], now, session["id"]),
            )
            if new_count == 1:
                self._insert_owner_outbox(
                    connection,
                    dedupe_key=f"session:{session['id']}:left-1",
                    session_id=str(session["id"]),
                    text=(
                        "Первое измерение слева получено: "
                        f"{_format_number(measurement['systolic'])}/"
                        f"{_format_number(measurement['diastolic'])}, пульс "
                        f"{_format_number(measurement['pulse'])}. "
                        "Сделайте второе измерение на левой руке."
                    ),
                    now=now,
                )
            elif new_count == 2:
                self._insert_owner_outbox(
                    connection,
                    dedupe_key=f"session:{session['id']}:switch-arm",
                    session_id=str(session["id"]),
                    text=(
                        "Второе измерение слева получено: "
                        f"{_format_number(measurement['systolic'])}/"
                        f"{_format_number(measurement['diastolic'])}, пульс "
                        f"{_format_number(measurement['pulse'])}. "
                        "Переставьте манжету на правую руку."
                    ),
                    now=now,
                )
            elif new_count == 3:
                self._insert_owner_outbox(
                    connection,
                    dedupe_key=f"session:{session['id']}:right-1",
                    session_id=str(session["id"]),
                    text=(
                        "Первое измерение справа получено: "
                        f"{_format_number(measurement['systolic'])}/"
                        f"{_format_number(measurement['diastolic'])}, пульс "
                        f"{_format_number(measurement['pulse'])}. "
                        "Сделайте второе измерение на правой руке."
                    ),
                    now=now,
                )
            return
        self._insert_owner_outbox(
            connection,
            dedupe_key=f"session:{session['id']}:right-2",
            session_id=str(session["id"]),
            text=(
                "Второе измерение справа получено: "
                f"{_format_number(measurement['systolic'])}/"
                f"{_format_number(measurement['diastolic'])}, пульс "
                f"{_format_number(measurement['pulse'])}. "
                "Серия завершена, итог отправляется в семейный канал."
            ),
            now=now,
        )
        self._complete_session(connection, str(session["id"]), now)

    def _assign_to_auto(
        self, connection: sqlite3.Connection, measurement: sqlite3.Row, now: int
    ) -> None:
        session = connection.execute(
            """
            SELECT * FROM sessions
            WHERE mode = 'auto_right' AND status = 'active'
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        if session is not None:
            first = connection.execute(
                """
                SELECT measured_at FROM measurements
                WHERE session_id = ? ORDER BY measured_at, id LIMIT 1
                """,
                (session["id"],),
            ).fetchone()
            delta = int(measurement["measured_at"]) - int(first["measured_at"])
            if delta < 0 or delta > self.settings.auto_pair_window_seconds:
                connection.execute(
                    """
                    UPDATE sessions SET status = 'timed_out', state = 'TIMED_OUT',
                        updated_at = ? WHERE id = ?
                    """,
                    (now, session["id"]),
                )
                session = None

        if session is None:
            session_id = str(uuid.uuid4())
            expires_at = (
                max(now, int(measurement["measured_at"])) + self.settings.auto_pair_window_seconds
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    id, mode, status, state, started_at, updated_at, expires_at
                ) VALUES (?, 'auto_right', 'active', 'WAIT_RIGHT_2', ?, ?, ?)
                """,
                (session_id, now, now, expires_at),
            )
            connection.execute(
                """
                UPDATE measurements
                SET session_id = ?, arm = 'right', processed_at = ?, processing_result = 'assigned'
                WHERE id = ?
                """,
                (session_id, now, measurement["id"]),
            )
            self._insert_owner_outbox(
                connection,
                dedupe_key=f"session:{session_id}:auto-right-1",
                session_id=session_id,
                text=(
                    "Первое измерение справа без /bp получено: "
                    f"{_format_number(measurement['systolic'])}/"
                    f"{_format_number(measurement['diastolic'])}, пульс "
                    f"{_format_number(measurement['pulse'])}. "
                    "Ожидаю второе измерение в течение часа."
                ),
                now=now,
            )
            return

        connection.execute(
            """
            UPDATE measurements
            SET session_id = ?, arm = 'right', processed_at = ?, processing_result = 'assigned'
            WHERE id = ?
            """,
            (session["id"], now, measurement["id"]),
        )
        self._insert_owner_outbox(
            connection,
            dedupe_key=f"session:{session['id']}:auto-right-2",
            session_id=str(session["id"]),
            text=(
                "Второе измерение справа без /bp получено: "
                f"{_format_number(measurement['systolic'])}/"
                f"{_format_number(measurement['diastolic'])}, пульс "
                f"{_format_number(measurement['pulse'])}. "
                "Итог отправляется в семейный канал."
            ),
            now=now,
        )
        self._complete_session(connection, str(session["id"]), now)

    def _complete_session(self, connection: sqlite3.Connection, session_id: str, now: int) -> None:
        connection.execute(
            """
            UPDATE sessions
            SET status = 'completed', state = 'COMPLETE', completed_at = ?,
                updated_at = ?, publish_status = 'pending'
            WHERE id = ?
            """,
            (now, now, session_id),
        )
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        rows = connection.execute(
            "SELECT * FROM measurements WHERE session_id = ? ORDER BY measured_at, id",
            (session_id,),
        ).fetchall()
        message = format_session_message(session, rows, self.settings)
        if self.settings.telegram_channel_id is None:
            connection.execute(
                "UPDATE sessions SET publish_status = 'blocked' WHERE id = ?", (session_id,)
            )
            self._insert_owner_outbox(
                connection,
                dedupe_key=f"session:{session_id}:missing-channel",
                session_id=session_id,
                text="Серия завершена, но TELEGRAM_CHANNEL_ID не настроен. Публикация остановлена.",
                now=now,
            )
            return
        self._insert_outbox(
            connection,
            dedupe_key=f"session:{session_id}:final",
            session_id=session_id,
            kind="final",
            chat_id=str(self.settings.telegram_channel_id),
            text=message,
            now=now,
        )

    def _insert_owner_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        dedupe_key: str,
        session_id: str | None,
        text: str,
        now: int,
    ) -> None:
        chat_id = self.settings.telegram_private_chat_id or self.settings.telegram_owner_user_id
        if chat_id is None:
            return
        self._insert_outbox(
            connection,
            dedupe_key=dedupe_key,
            session_id=session_id,
            kind="owner",
            chat_id=str(chat_id),
            text=text,
            now=now,
        )

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        dedupe_key: str,
        session_id: str | None,
        kind: str,
        chat_id: str,
        text: str,
        now: int,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO telegram_outbox(
                dedupe_key, session_id, kind, chat_id, message_text,
                status, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (dedupe_key, session_id, kind, chat_id, text, now, now),
        )

    async def process_outbox_once(self) -> bool:
        if not self.settings.telegram_bot_token:
            return False
        now = self.now()
        async with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM telegram_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE telegram_outbox SET status = 'sending' WHERE id = ?", (row["id"],)
            )
            outbox = dict(row)

        try:
            sent = await self.telegram.send_message(
                outbox["chat_id"], str(outbox["message_text"]), silent=self.settings.telegram_silent
            )
        except TelegramDefinitiveError as error:
            attempts = int(outbox["attempts"]) + 1
            retry_after = error.retry_after or min(300, 2 ** min(attempts, 8))
            status = "failed" if attempts >= 8 else "pending"
            async with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE telegram_outbox
                    SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (status, attempts, now + retry_after, _safe_error(error), outbox["id"]),
                )
                if status == "failed" and outbox["kind"] == "final":
                    connection.execute(
                        "UPDATE sessions SET publish_status = 'failed' WHERE id = ?",
                        (outbox["session_id"],),
                    )
            logger.warning("telegram_send_failed", extra={"attempts": attempts, "status": status})
            return True
        except TelegramAmbiguousError as error:
            async with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE telegram_outbox SET status = 'uncertain', last_error = ? WHERE id = ?
                    """,
                    (_safe_error(error), outbox["id"]),
                )
                if outbox["kind"] == "final":
                    connection.execute(
                        "UPDATE sessions SET publish_status = 'uncertain' WHERE id = ?",
                        (outbox["session_id"],),
                    )
                    self._insert_owner_outbox(
                        connection,
                        dedupe_key=f"outbox:{outbox['id']}:uncertain",
                        session_id=outbox["session_id"],
                        text=(
                            "Telegram не подтвердил итоговую публикацию. Автоповтор отключен, "
                            "чтобы не создать дубликат. Проверьте канал и /status."
                        ),
                        now=now,
                    )
            logger.error("telegram_send_uncertain")
            return True

        async with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE telegram_outbox
                SET status = 'sent', sent_at = ?, telegram_message_id = ?, last_error = NULL
                WHERE id = ?
                """,
                (self.now(), sent.message_id, outbox["id"]),
            )
            if outbox["kind"] == "final":
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'published', publish_status = 'sent', telegram_message_id = ?,
                        updated_at = ? WHERE id = ?
                    """,
                    (sent.message_id, self.now(), outbox["session_id"]),
                )
        logger.info("telegram_message_sent", extra={"kind": outbox["kind"]})
        return True

    async def start_manual_session(self, owner_user_id: int) -> str:
        now = self.now()
        session_id = str(uuid.uuid4())
        async with self.database.transaction() as connection:
            self._expire_sessions_in_transaction(connection, now)
            existing = connection.execute(
                """
                SELECT state FROM sessions
                WHERE mode = 'four_arm' AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            if existing is not None:
                return f"Серия уже идет: {state_description(str(existing['state']))}."
            connection.execute(
                """
                UPDATE sessions SET status = 'cancelled', state = 'CANCELLED',
                    cancelled_at = ?, updated_at = ?
                WHERE mode = 'auto_right' AND status = 'active'
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    id, mode, status, state, started_at, updated_at, expires_at,
                    created_by_user_id
                ) VALUES (?, 'four_arm', 'active', 'WAIT_LEFT_1', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    now,
                    now,
                    now + self.settings.session_timeout_minutes * 60,
                    owner_user_id,
                ),
            )
        return "Серия начата. Сделайте первое измерение на левой руке."

    async def cancel_manual_session(self) -> str:
        now = self.now()
        async with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT id FROM sessions
                WHERE mode = 'four_arm' AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return "Активной /bp-сессии нет."
            connection.execute(
                """
                UPDATE sessions SET status = 'cancelled', state = 'CANCELLED',
                    cancelled_at = ?, updated_at = ? WHERE id = ?
                """,
                (now, now, row["id"]),
            )
        return "Серия отменена. Данные не будут опубликованы."

    async def retry_manual_session(self, owner_user_id: int) -> str:
        await self.cancel_manual_session()
        return await self.start_manual_session(owner_user_id)

    async def session_status_text(self) -> str:
        await self.expire_sessions()
        async with self.database.read() as connection:
            manual = connection.execute(
                """
                SELECT * FROM sessions
                WHERE mode = 'four_arm' AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            if manual is not None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM measurements WHERE session_id = ?", (manual["id"],)
                ).fetchone()[0]
                remaining = max(0, int(manual["expires_at"]) - self.now())
                return (
                    f"Серия /bp: {state_description(str(manual['state']))}. "
                    f"Получено {count}/4, осталось {remaining // 60}:{remaining % 60:02d}."
                )
            auto = connection.execute(
                """
                SELECT * FROM sessions
                WHERE mode = 'auto_right' AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            if auto is not None:
                return "Без /bp: получено 1/2 для правой руки, ожидается второе измерение."
            latest = connection.execute(
                "SELECT status, publish_status FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if latest is None:
            return "IDLE. Активной серии нет."
        return f"IDLE. Последняя серия: {latest['status']}, публикация: {latest['publish_status']}."

    async def handle_command(
        self, *, owner_user_id: int, chat_id: int, chat_type: str, text: str
    ) -> str | None:
        if self.settings.telegram_owner_user_id is None:
            return None
        if owner_user_id != self.settings.telegram_owner_user_id or chat_type != "private":
            return None
        if (
            self.settings.telegram_private_chat_id is not None
            and chat_id != self.settings.telegram_private_chat_id
        ):
            return None
        command = text.strip().split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
        if command == "/bp":
            return await self.start_manual_session(owner_user_id)
        if command == "/cancel":
            return await self.cancel_manual_session()
        if command == "/retry":
            return await self.retry_manual_session(owner_user_id)
        if command == "/status":
            return await self.session_status_text()
        return None

    async def process_telegram_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        message = update.get("message")
        if update_id is None or not isinstance(message, dict):
            return
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return
        try:
            response = await self.handle_command(
                owner_user_id=int(sender.get("id")),
                chat_id=int(chat.get("id")),
                chat_type=str(chat.get("type", "")),
                text=text,
            )
        except (TypeError, ValueError):
            return
        if response is None:
            return
        now = self.now()
        async with self.database.transaction() as connection:
            self._insert_outbox(
                connection,
                dedupe_key=f"telegram-update:{update_id}:response",
                session_id=None,
                kind="owner",
                chat_id=str(chat["id"]),
                text=response,
                now=now,
            )

    async def expire_sessions(self) -> int:
        now = self.now()
        async with self.database.transaction() as connection:
            return self._expire_sessions_in_transaction(connection, now)

    def _expire_sessions_in_transaction(self, connection: sqlite3.Connection, now: int) -> int:
        expired = connection.execute(
            "SELECT * FROM sessions WHERE status = 'active' AND expires_at <= ?", (now,)
        ).fetchall()
        for session in expired:
            connection.execute(
                """
                UPDATE sessions SET status = 'timed_out', state = 'TIMED_OUT', updated_at = ?
                WHERE id = ?
                """,
                (now, session["id"]),
            )
            if session["mode"] == "four_arm":
                self._insert_owner_outbox(
                    connection,
                    dedupe_key=f"session:{session['id']}:timeout",
                    session_id=str(session["id"]),
                    text="Время серии истекло. Неполная серия не опубликована; начните заново командой /retry.",
                    now=now,
                )
        return len(expired)

    async def enqueue_test_message(self, target: str) -> None:
        now = self.now()
        if target == "channel":
            chat_id = self.settings.telegram_channel_id
            text = "BP Reporter: тестовая тихая публикация."
        elif target == "private":
            chat_id = self.settings.telegram_private_chat_id or self.settings.telegram_owner_user_id
            text = "BP Reporter подключен. Тестовое тихое сообщение."
        else:
            raise ValueError("target must be private or channel")
        if chat_id is None:
            raise ValueError("Telegram chat ID is not configured")
        async with self.database.transaction() as connection:
            self._insert_outbox(
                connection,
                dedupe_key=f"test:{target}:{uuid.uuid4()}",
                session_id=None,
                kind="test",
                chat_id=str(chat_id),
                text=text,
                now=now,
            )

    async def get_state(self, key: str) -> str | None:
        async with self.database.read() as connection:
            row = connection.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    async def set_state(self, key: str, value: str) -> None:
        async with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, self.now()),
            )

    async def recent_measurements(self, limit: int) -> list[dict[str, Any]]:
        async with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT id, userid, grpid, measured_at, systolic, diastolic, pulse,
                    device_id, model_id, source, processed_at, processing_result,
                    session_id, arm
                FROM measurements ORDER BY measured_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def debug_session(self) -> dict[str, Any]:
        async with self.database.read() as connection:
            session = connection.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if session is None:
                return {"state": "IDLE", "session": None, "measurements": []}
            measurements = connection.execute(
                """
                SELECT id, measured_at, systolic, diastolic, pulse, arm, processing_result
                FROM measurements WHERE session_id = ? ORDER BY measured_at, id
                """,
                (session["id"],),
            ).fetchall()
        return {
            "state": session["state"],
            "session": dict(session),
            "measurements": [dict(row) for row in measurements],
        }


def format_session_message(
    session: sqlite3.Row | dict[str, Any],
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    settings: Settings,
) -> str:
    if not rows:
        raise ValueError("cannot format an empty session")
    latest = max(int(row["measured_at"]) for row in rows)
    timestamp = datetime.fromtimestamp(latest, tz=UTC).astimezone(settings.display_timezone)
    lines = [f"Давление — {timestamp:%d.%m.%Y, %H:%M}", ""]
    mode = str(session["mode"])
    left = [row for row in rows if row["arm"] == "left"]
    right = [row for row in rows if row["arm"] == "right"]
    if mode == "four_arm":
        if len(left) != 2 or len(right) != 2:
            raise ValueError("four-arm session must contain two measurements per arm")
        left_values = _arm_values(left)
        right_values = _arm_values(right)
        lines.extend(_arm_lines("Левая", left, left_values))
        lines.append("")
        lines.extend(_arm_lines("Правая", right, right_values))
        sys_diff = right_values[0] - left_values[0]
        dia_diff = right_values[1] - left_values[1]
        lines.extend(["", f"Разница П−Л: {_signed(sys_diff)}/{_signed(dia_diff)} мм рт. ст."])
    else:
        if len(right) != 2:
            raise ValueError("automatic right-arm session must contain two measurements")
        lines.extend(_arm_lines("Правая", right, _arm_values(right)))
    lines.extend(["", "<i>Тонометр Whithings</i>"])
    return "\n".join(lines)


def _arm_values(rows: list[sqlite3.Row] | list[dict[str, Any]]) -> tuple[int, int, int]:
    return tuple(
        _round_half_up(sum(Decimal(str(row[field])) for row in rows) / len(rows))
        for field in ("systolic", "diastolic", "pulse")
    )  # type: ignore[return-value]


def _arm_lines(
    title: str,
    rows: list[sqlite3.Row] | list[dict[str, Any]],
    averages: tuple[int, int, int],
) -> list[str]:
    originals = " · ".join(
        f"{_format_number(row['systolic'])}/{_format_number(row['diastolic'])}/"
        f"{_format_number(row['pulse'])}"
        for row in rows
    )
    return [
        f"{title}: {averages[0]}/{averages[1]}, пульс {averages[2]}",
        originals,
    ]


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _format_number(value: Any) -> str:
    decimal = Decimal(str(value))
    if decimal == decimal.to_integral_value():
        return str(int(decimal))
    return format(decimal.normalize(), "f")


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _decimal_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _safe_error(error: BaseException) -> str:
    if isinstance(error, WithingsError) and error.status is not None:
        return f"Withings API status {error.status}"
    return error.__class__.__name__


def state_description(state: str) -> str:
    return {
        "WAIT_LEFT_1": "ожидается первое измерение слева",
        "WAIT_LEFT_2": "ожидается второе измерение слева",
        "WAIT_RIGHT_1": "ожидается первое измерение справа",
        "WAIT_RIGHT_2": "ожидается второе измерение справа",
        "COMPLETE": "серия завершена",
    }.get(state, state)
