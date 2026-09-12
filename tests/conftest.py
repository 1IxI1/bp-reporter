from __future__ import annotations

from typing import Any

import pytest

from app.clients import SentMessage
from app.config import Settings, make_test_settings
from app.db import Database
from app.models import FetchResult
from app.service import BPService


class FakeWithings:
    def __init__(self, groups: list[dict[str, Any]] | None = None) -> None:
        self.groups = groups or []
        self.fetch_calls: list[dict[str, Any]] = []
        self.subscribed = False

    async def close(self) -> None:
        pass

    async def resolve_userid(self) -> str:
        return "42"

    async def fetch_measurements(self, userid: str, **kwargs: Any) -> FetchResult:
        self.fetch_calls.append({"userid": userid, **kwargs})
        return FetchResult(groups=self.groups, updatetime=None)

    async def list_subscriptions(self, userid: str) -> list[dict[str, Any]]:
        return []

    async def subscribe_to_blood_pressure(self, userid: str) -> None:
        self.subscribed = True


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.command_menus: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, str]] = []
        self.removed_keyboards: list[dict[str, int | str]] = []

    async def close(self) -> None:
        pass

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        silent: bool | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> SentMessage:
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "silent": silent,
                "reply_markup": reply_markup,
            }
        )
        return SentMessage(message_id=len(self.messages), chat_id=chat_id)

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
    ) -> SentMessage:
        self.edited_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return SentMessage(message_id=message_id, chat_id=chat_id)

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.callback_answers.append({"id": callback_query_id, "text": text})

    async def remove_inline_keyboard(self, chat_id: int | str, message_id: int) -> None:
        self.removed_keyboards.append({"chat_id": chat_id, "message_id": message_id})

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        return []

    async def get_me(self) -> dict[str, Any]:
        return {"id": 1, "username": "test_bot"}

    async def set_commands(
        self,
        chat_id: int | str,
        commands: list[dict[str, str]],
    ) -> None:
        self.command_menus.append({"chat_id": chat_id, "commands": commands})


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_test_settings(
        tmp_path,
        withings_user_id="42",
        telegram_bot_token="test-token",
        live_after="2026-01-01T00:00:00Z",
    )


@pytest.fixture
async def service(settings: Settings) -> BPService:
    database = Database(settings.database_path)
    await database.initialize()
    return BPService(settings, database, FakeWithings(), FakeTelegram())  # type: ignore[arg-type]


def measure_group(
    grpid: int,
    timestamp: int,
    systolic: int,
    diastolic: int,
    pulse: int,
    *,
    unit: int = 0,
) -> dict[str, Any]:
    multiplier = 10 ** (-unit)
    return {
        "grpid": grpid,
        "date": timestamp,
        "created": timestamp + 1,
        "modified": timestamp + 2,
        "deviceid": "wpm05-device",
        "model_id": 45,
        "model": "BPM Connect",
        "measures": [
            {"type": 10, "value": systolic * multiplier, "unit": unit},
            {"type": 9, "value": diastolic * multiplier, "unit": unit},
            {"type": 11, "value": pulse * multiplier, "unit": unit},
        ],
    }
