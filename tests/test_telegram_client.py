from __future__ import annotations

import json
from typing import Any

import httpx

from app.clients import TelegramClient
from app.config import make_test_settings


async def test_inline_keyboard_and_callback_api_payloads(tmp_path) -> None:
    settings = make_test_settings(tmp_path, telegram_bot_token="test-token")
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append((request.url.path, body))
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelegramClient(settings, http)
    reply_markup = {
        "inline_keyboard": [[{"text": "Начать полный цикл", "callback_data": "bp-full:1"}]]
    }

    await client.send_message(100, "text", reply_markup=reply_markup)
    await client.get_updates(20)
    await client.answer_callback_query("callback-1", "started")
    await client.remove_inline_keyboard(100, 10)
    await http.aclose()

    assert calls[0][0].endswith("/sendMessage")
    assert calls[0][1]["reply_markup"] == reply_markup
    assert calls[1][1]["allowed_updates"] == ["message", "callback_query"]
    assert calls[2][1] == {"callback_query_id": "callback-1", "text": "started"}
    assert calls[3][1]["reply_markup"] == {"inline_keyboard": []}
