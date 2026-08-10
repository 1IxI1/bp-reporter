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
    final = telegram.messages[-1]
    assert final["chat_id"] == "-100200"
    assert final["silent"] is True
    assert "Левая: 146/90, пульс 71" in final["text"]
    assert "148/91/72 · 143/89/70" in final["text"]
    assert "Правая: 149/91, пульс 72" in final["text"]
    assert "Разница П−Л: +3/+1 мм рт. ст." in final["text"]

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

    assert await service.process_outbox_once()
    telegram = service.telegram
    assert isinstance(telegram, FakeTelegram)
    assert "Правая: 141/87, пульс 69" in telegram.messages[0]["text"]
    assert "Левая" not in telegram.messages[0]["text"]


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
