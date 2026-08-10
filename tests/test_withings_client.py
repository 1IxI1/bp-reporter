from __future__ import annotations

import json
import sqlite3

import httpx

from app.clients import WithingsClient
from app.config import make_test_settings
from app.db import Database


async def test_refresh_rotation_and_getmeas_pagination(tmp_path) -> None:
    settings = make_test_settings(
        tmp_path,
        withings_client_id="client",
        withings_client_secret="secret",
        withings_user_id="42",
    )
    database = Database(settings.database_path)
    await database.initialize()
    async with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO oauth_tokens(
                userid, access_token, refresh_token, expires_at, scope, updated_at
            ) VALUES ('42', 'old-access', 'old-refresh', 1, 'user.metrics', 1)
            """
        )

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        form = dict(item.split("=", maxsplit=1) for item in request.content.decode().split("&"))
        if request.url.path == "/v2/oauth2":
            assert form["refresh_token"] == "old-refresh"
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "body": {
                        "userid": 42,
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 10800,
                        "scope": "user.metrics",
                    },
                },
            )
        assert request.headers["Authorization"] == "Bearer new-access"
        if "offset" not in form:
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "body": {
                        "measuregrps": [{"grpid": 1, "date": 1, "measures": []}],
                        "more": 1,
                        "offset": 10,
                        "updatetime": 20,
                    },
                },
            )
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "status": 0,
                    "body": {
                        "measuregrps": [{"grpid": 2, "date": 2, "measures": []}],
                        "more": 0,
                        "updatetime": 21,
                    },
                }
            ),
            headers={"content-type": "application/json"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WithingsClient(settings, database, http)
    result = await client.fetch_measurements("42", lastupdate=100)
    await http.aclose()

    assert [group["grpid"] for group in result.groups] == [1, 2]
    assert result.updatetime == 21
    with sqlite3.connect(settings.database_path) as connection:
        tokens = connection.execute(
            "SELECT access_token, refresh_token FROM oauth_tokens WHERE userid = '42'"
        ).fetchone()
    assert tokens == ("new-access", "new-refresh")
    assert len(calls) == 3
