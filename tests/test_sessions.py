from __future__ import annotations

from app.service import BPService
from tests.conftest import FakeTelegram, measure_group


async def test_four_measurement_session_and_deduplication(service: BPService) -> None:
    now = 1_786_000_000
    service.now = lambda: now  # type: ignore[method-assign]

    response = await service.start_manual_session(100)
    assert response.startswith("Серия начата")

    groups = [
        measure_group(1, now + 1, 148, 91, 72),
        measure_group(2, now + 2, 143, 89, 70),
        measure_group(3, now + 3, 151, 92, 73),
        measure_group(4, now + 4, 147, 90, 71),
    ]
    for group in groups:
        stats = await service.ingest_groups("42", [group], source="poll")
        assert stats["inserted"] == 1

    debug = await service.debug_session()
    assert debug["state"] == "COMPLETE"
    assert [row["arm"] for row in debug["measurements"]] == ["left", "left", "right", "right"]

    duplicate_stats = await service.ingest_groups("42", groups, source="webhook")
    assert duplicate_stats == {"fetched": 4, "inserted": 0, "duplicates": 4, "invalid": 0}
    debug = await service.debug_session()
    assert len(debug["measurements"]) == 4

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert await service.process_outbox_once()  # first-left acknowledgement
    assert await service.process_outbox_once()  # switch-arm prompt
    assert await service.process_outbox_once()  # first-right acknowledgement
    assert await service.process_outbox_once()  # second-right acknowledgement
    assert await service.process_outbox_once()  # final channel post
    assert len(telegram.messages) == 5
    assert "Первое измерение слева получено: 148/91, пульс 72" in telegram.messages[0]["text"]
    assert "Второе измерение слева получено: 143/89, пульс 70" in telegram.messages[1]["text"]
    assert "Первое измерение справа получено: 151/92, пульс 73" in telegram.messages[2]["text"]
    assert "Второе измерение справа получено: 147/90, пульс 71" in telegram.messages[3]["text"]
    assert all(message["silent"] is True for message in telegram.messages[:-1])
    final = telegram.messages[-1]
    assert final["chat_id"] == "-100200"
    assert final["silent"] is False
    assert "Левая: 146/90, пульс 71" in final["text"]
    assert "148/91/72 · 143/89/70" in final["text"]
    assert "Правая: 149/91, пульс 72" in final["text"]
    assert "Разница П−Л: +3/+1 мм рт. ст." in final["text"]
    assert final["text"].endswith("<i>Тонометр Whithings</i>")

    debug = await service.debug_session()
    assert debug["session"]["status"] == "published"
    assert debug["session"]["telegram_message_id"] == 5


async def test_two_unsolicited_measurements_publish_as_right_arm(service: BPService) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    await service.ingest_groups("42", [measure_group(10, clock[0], 140, 86, 68)], source="poll")
    status = await service.session_status_text()
    assert "получено 1/2" in status

    clock[0] += 3_599
    await service.ingest_groups("42", [measure_group(11, clock[0], 142, 88, 70)], source="poll")
    debug = await service.debug_session()
    assert debug["session"]["mode"] == "auto_right"
    assert debug["session"]["status"] == "completed"
    assert [row["arm"] for row in debug["measurements"]] == ["right", "right"]

    assert await service.process_outbox_once()  # first-right acknowledgement
    assert await service.process_outbox_once()  # second-right acknowledgement
    assert await service.process_outbox_once()  # final channel post
    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert (
        "Первое измерение справа без /bp получено: 140/86, пульс 68" in telegram.messages[0]["text"]
    )
    button = telegram.messages[0]["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Начать полный цикл"
    assert button["callback_data"].startswith("bp-full:")
    assert (
        "Второе измерение справа без /bp получено: 142/88, пульс 70" in telegram.messages[1]["text"]
    )
    assert "Правая: 141/87, пульс 69" in telegram.messages[2]["text"]
    assert "Левая" not in telegram.messages[2]["text"]
    assert telegram.messages[2]["text"].endswith("<i>Тонометр Whithings</i>")


async def test_consecutive_auto_pairs_edit_first_report_as_two_arm_cycle(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    await service.ingest_groups("42", [measure_group(30, clock[0], 147, 77, 75)], source="poll")
    clock[0] += 60
    await service.ingest_groups("42", [measure_group(31, clock[0], 145, 75, 73)], source="poll")
    for _ in range(3):
        assert await service.process_outbox_once()

    clock[0] += 13 * 60
    await service.ingest_groups("42", [measure_group(32, clock[0], 147, 78, 78)], source="poll")
    clock[0] += 2 * 60
    await service.ingest_groups("42", [measure_group(33, clock[0], 141, 77, 73)], source="poll")
    for _ in range(3):
        assert await service.process_outbox_once()

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    channel_messages = [message for message in telegram.messages if message["chat_id"] == "-100200"]
    assert len(channel_messages) == 1
    assert len(telegram.edited_messages) == 1
    edit = telegram.edited_messages[0]
    assert edit["chat_id"] == "-100200"
    assert edit["message_id"] == 3
    edited = edit["text"]
    assert "Левая: 146/76, пульс 74" in edited
    assert "147/77/75 · 145/75/73" in edited
    assert "Правая: 144/78, пульс 76" in edited
    assert "147/78/78 · 141/77/73" in edited

    async with service.database.read() as connection:
        combined = connection.execute(
            "SELECT id, status, mode, telegram_message_id FROM sessions "
            "WHERE mode = 'auto_four_arm'"
        ).fetchone()
        merged = connection.execute(
            "SELECT status, publish_status FROM sessions WHERE state = 'MERGED'"
        ).fetchone()
        arms = connection.execute(
            "SELECT arm FROM measurements WHERE session_id = ? ORDER BY measured_at, id",
            (combined["id"],),
        ).fetchall()
    assert combined["status"] == "published"
    assert combined["telegram_message_id"] == 3
    assert dict(merged) == {"status": "merged", "publish_status": "merged"}
    assert [row["arm"] for row in arms] == ["left", "left", "right", "right"]

    duplicate = await service.ingest_groups(
        "42", [measure_group(33, clock[0], 141, 77, 73)], source="webhook"
    )
    assert duplicate == {"fetched": 1, "inserted": 0, "duplicates": 1, "invalid": 0}
    assert not await service.process_outbox_once()
    assert len(telegram.edited_messages) == 1


async def test_consecutive_auto_pairs_merge_before_first_report_is_sent(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid, systolic in ((40, 140), (41, 142)):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], systolic, 80, 70)], source="poll"
        )
        clock[0] += 60
    retry_at = clock[0] + 2 * 60
    async with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE telegram_outbox
            SET attempts = 3, next_attempt_at = ?, last_error = 'TelegramDefinitiveError'
            WHERE kind = 'final'
            """,
            (retry_at,),
        )
    for grpid, systolic in ((42, 150), (43, 148)):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], systolic, 90, 72)], source="poll"
        )
        clock[0] += 60

    while await service.process_outbox_once():
        pass

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    channel_messages = [message for message in telegram.messages if message["chat_id"] == "-100200"]
    assert len(channel_messages) == 1
    assert "Левая: 141/80, пульс 70" in channel_messages[0]["text"]
    assert "Правая: 149/90, пульс 72" in channel_messages[0]["text"]
    assert telegram.messages[-1] == channel_messages[0]
    assert telegram.edited_messages == []
    async with service.database.read() as connection:
        final_rows = connection.execute(
            """
            SELECT status, attempts, next_attempt_at, last_error
            FROM telegram_outbox WHERE kind = 'final' ORDER BY id
            """
        ).fetchall()
    assert [row["status"] for row in final_rows] == ["superseded", "sent"]
    assert [row["attempts"] for row in final_rows] == [3, 3]
    assert [row["next_attempt_at"] for row in final_rows] == [retry_at, retry_at]
    assert final_rows[1]["last_error"] is None


async def test_auto_pairs_more_than_fifteen_minutes_apart_publish_separately(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid in (50, 51):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 140, 80, 70)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    clock[0] += 15 * 60
    for grpid in (52, 53):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 150, 90, 72)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    channel_messages = [message for message in telegram.messages if message["chat_id"] == "-100200"]
    assert len(channel_messages) == 2
    assert telegram.edited_messages == []


async def test_auto_pairs_do_not_merge_across_an_intervening_manual_cycle(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid in (60, 61):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 140, 80, 70)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    await service.start_manual_session(100)
    for grpid in (62, 63, 64, 65):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 145, 85, 72)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    for grpid in (66, 67):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 150, 90, 74)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    channel_messages = [message for message in telegram.messages if message["chat_id"] == "-100200"]
    assert len(channel_messages) == 3
    assert telegram.edited_messages == []


async def test_overlapping_latest_cycle_blocks_merge_with_an_older_pair(
    service: BPService,
) -> None:
    base = 1_786_000_000
    clock = [base]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid in (68, 69):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 140, 80, 70)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    clock[0] += 16 * 60
    for grpid in (70, 71):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 145, 85, 72)], source="poll"
        )
        clock[0] += 1
    while await service.process_outbox_once():
        pass

    async with service.database.transaction() as connection:
        sessions = connection.execute(
            "SELECT id FROM sessions WHERE mode = 'auto_right' ORDER BY started_at"
        ).fetchall()
        for session_id, timestamps in zip(
            (sessions[0]["id"], sessions[1]["id"]),
            ((base + 100, base + 250), (base + 200, base + 300)),
            strict=True,
        ):
            measurements = connection.execute(
                "SELECT id FROM measurements WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            for measurement, measured_at in zip(measurements, timestamps, strict=True):
                connection.execute(
                    "UPDATE measurements SET measured_at = ? WHERE id = ?",
                    (measured_at, measurement["id"]),
                )
        connection.execute(
            "UPDATE sessions SET publish_status = 'uncertain' WHERE id = ?",
            (sessions[1]["id"],),
        )

    clock[0] = base + 350
    await service.ingest_groups("42", [measure_group(72, base + 275, 150, 90, 74)], source="poll")
    await service.ingest_groups("42", [measure_group(73, base + 350, 148, 88, 72)], source="poll")
    while await service.process_outbox_once():
        pass

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    channel_messages = [message for message in telegram.messages if message["chat_id"] == "-100200"]
    assert len(channel_messages) == 3
    assert telegram.edited_messages == []


async def test_auto_pair_merge_is_reconciled_after_first_report_finishes_sending(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid in (70, 71):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 140, 80, 70)], source="poll"
        )
        clock[0] += 1
    assert await service.process_outbox_once()
    assert await service.process_outbox_once()

    async with service.database.transaction() as connection:
        first = connection.execute(
            "SELECT id FROM sessions WHERE mode = 'auto_right' ORDER BY started_at LIMIT 1"
        ).fetchone()
        final = connection.execute(
            "SELECT id FROM telegram_outbox WHERE session_id = ? AND kind = 'final'",
            (first["id"],),
        ).fetchone()
        connection.execute(
            "UPDATE telegram_outbox SET status = 'sending' WHERE id = ?", (final["id"],)
        )

    for grpid in (72, 73):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 150, 90, 72)], source="poll"
        )
        clock[0] += 1

    async with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE telegram_outbox
            SET status = 'sent', telegram_message_id = 3, sent_at = ?
            WHERE id = ?
            """,
            (clock[0], final["id"]),
        )
        connection.execute(
            """
            UPDATE sessions
            SET status = 'published', publish_status = 'sent', telegram_message_id = 3
            WHERE id = ?
            """,
            (first["id"],),
        )

    while await service.process_outbox_once():
        pass

    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert len(telegram.edited_messages) == 1
    assert telegram.edited_messages[0]["message_id"] == 3
    assert "Левая: 140/80, пульс 70" in telegram.edited_messages[0]["text"]
    assert "Правая: 150/90, пульс 72" in telegram.edited_messages[0]["text"]


async def test_interrupted_final_send_is_marked_uncertain_and_warns_owner(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]

    for grpid in (80, 81):
        await service.ingest_groups(
            "42", [measure_group(grpid, clock[0], 140, 80, 70)], source="poll"
        )
        clock[0] += 1
    assert await service.process_outbox_once()
    assert await service.process_outbox_once()
    async with service.database.transaction() as connection:
        connection.execute("UPDATE telegram_outbox SET status = 'sending' WHERE kind = 'final'")

    await service.database.initialize()
    assert await service.recover_interrupted_outbox() == 1
    assert await service.recover_interrupted_outbox() == 0

    async with service.database.read() as connection:
        session = connection.execute(
            "SELECT publish_status FROM sessions WHERE mode = 'auto_right'"
        ).fetchone()
    assert session["publish_status"] == "uncertain"
    assert await service.process_outbox_once()
    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert "Telegram не подтвердил итоговую публикацию" in telegram.messages[-1]["text"]


async def test_inline_button_promotes_first_auto_measurement_to_full_cycle(
    service: BPService,
) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]
    await service.ingest_groups("42", [measure_group(20, clock[0], 140, 86, 68)], source="poll")

    assert await service.process_outbox_once()
    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    button = telegram.messages[0]["reply_markup"]["inline_keyboard"][0][0]
    callback = {
        "update_id": 500,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 100},
            "message": {
                "message_id": 1,
                "chat": {"id": 100, "type": "private"},
            },
            "data": button["callback_data"],
        },
    }

    unauthorized = {
        **callback,
        "update_id": 499,
        "callback_query": {
            **callback["callback_query"],
            "id": "callback-unauthorized",
            "from": {"id": 999},
        },
    }
    await service.process_telegram_update(unauthorized)
    debug = await service.debug_session()
    assert debug["session"]["mode"] == "auto_right"
    assert telegram.callback_answers == []

    await service.process_telegram_update(callback)
    debug = await service.debug_session()
    assert debug["session"]["mode"] == "four_arm"
    assert debug["session"]["state"] == "WAIT_LEFT_2"
    assert [row["arm"] for row in debug["measurements"]] == ["left"]
    assert telegram.callback_answers == [
        {
            "id": "callback-1",
            "text": "Полный цикл начат: первое измерение засчитано слева.",
        }
    ]
    assert telegram.removed_keyboards == [{"chat_id": 100, "message_id": 1}]

    assert await service.process_outbox_once()
    assert "Сделайте второе измерение на левой руке" in telegram.messages[1]["text"]

    for grpid, systolic, diastolic, pulse in (
        (21, 142, 88, 70),
        (22, 150, 90, 72),
        (23, 148, 89, 71),
    ):
        clock[0] += 1
        await service.ingest_groups(
            "42",
            [measure_group(grpid, clock[0], systolic, diastolic, pulse)],
            source="poll",
        )

    debug = await service.debug_session()
    assert debug["session"]["status"] == "completed"
    assert [row["arm"] for row in debug["measurements"]] == [
        "left",
        "left",
        "right",
        "right",
    ]

    for _ in range(4):
        assert await service.process_outbox_once()
    final = telegram.messages[-1]
    assert final["chat_id"] == "-100200"
    assert "Левая: 141/87, пульс 69" in final["text"]
    assert "Правая: 149/90, пульс 72" in final["text"]

    await service.process_telegram_update(callback)
    debug = await service.debug_session()
    assert len(debug["measurements"]) == 4
    assert telegram.callback_answers[-1]["text"].startswith("Серия уже завершена")


async def test_backfill_is_stored_but_not_assigned(service: BPService) -> None:
    now = 1_767_225_700
    service.now = lambda: now  # type: ignore[method-assign]
    group = measure_group(20, 1_767_225_500, 140, 90, 70)

    stats = await service.ingest_groups("42", [group], source="poll")

    assert stats["inserted"] == 1
    recent = await service.recent_measurements(10)
    assert recent[0]["processing_result"] == "backfill"
    assert recent[0]["session_id"] is None


async def test_manual_session_times_out_without_publication(service: BPService) -> None:
    clock = [1_786_000_000]
    service.now = lambda: clock[0]  # type: ignore[method-assign]
    await service.start_manual_session(100)
    await service.ingest_groups("42", [measure_group(30, clock[0] + 1, 140, 90, 70)], source="poll")

    clock[0] += 20 * 60 + 1
    assert await service.expire_sessions() == 1
    debug = await service.debug_session()
    assert debug["session"]["status"] == "timed_out"
    assert debug["session"]["publish_status"] == "none"

    assert await service.process_outbox_once()  # first-left acknowledgement
    assert await service.process_outbox_once()  # timeout notification
    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert "Неполная серия не опубликована" in telegram.messages[1]["text"]
