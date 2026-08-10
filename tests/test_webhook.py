from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.config import make_test_settings
from app.main import create_app
from tests.conftest import FakeTelegram, FakeWithings


def test_webhook_head_and_duplicate_enqueue(tmp_path) -> None:
    settings = make_test_settings(
        tmp_path,
        withings_user_id="42",
        telegram_bot_token="test-token",
    )
    withings = FakeWithings()
    telegram = FakeTelegram()
    app = create_app(
        settings,
        withings_client=withings,  # type: ignore[arg-type]
        telegram_client=telegram,  # type: ignore[arg-type]
        start_workers=False,
    )
    with TestClient(app) as client:
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute(
                """
                INSERT INTO oauth_tokens(
                    userid, access_token, refresh_token, expires_at, scope, updated_at
                ) VALUES ('42', 'access', 'refresh', 9999999999, 'user.metrics', 1)
                """
            )
        assert client.head("/withings/webhook?token=wrong").status_code == 403
        assert client.head("/withings/webhook?token=test-webhook-secret").status_code == 200

        payload = {"userid": "42", "appli": "4", "startdate": "100", "enddate": "200"}
        first = client.post("/withings/webhook?token=test-webhook-secret", data=payload)
        second = client.post("/withings/webhook?token=test-webhook-secret", data=payload)
        assert first.status_code == 202
        assert first.json() == {"queued": True}
        assert second.json() == {"queued": False}

        with sqlite3.connect(settings.database_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*), MAX(duplicate_count) FROM webhook_events"
            ).fetchone()
        assert row == (1, 1)


def test_debug_endpoints_require_admin_secret(tmp_path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(
        settings,
        withings_client=FakeWithings(),  # type: ignore[arg-type]
        telegram_client=FakeTelegram(),  # type: ignore[arg-type]
        start_workers=False,
    )
    with TestClient(app) as client:
        assert client.get("/debug/session").status_code == 401
        response = client.get("/debug/session", headers={"X-Admin-Secret": "test-admin-secret"})
        assert response.status_code == 200
        assert response.json()["state"] == "IDLE"
