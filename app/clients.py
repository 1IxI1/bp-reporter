from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.db import Database
from app.models import FetchResult

WITHINGS_BASE_URL = "https://wbsapi.withings.net"
WITHINGS_AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"


class WithingsError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class TelegramError(RuntimeError):
    pass


class TelegramDefinitiveError(TelegramError):
    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramAmbiguousError(TelegramError):
    """The request may have reached Telegram and must not be retried automatically."""


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int
    chat_id: int | str


class WithingsClient:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.http = http_client or httpx.AsyncClient(timeout=20)
        self._owns_http = http_client is None
        self._refresh_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def exchange_code(self, code: str) -> str:
        body = await self._token_request(
            {
                "action": "requesttoken",
                "client_id": self.settings.withings_client_id,
                "client_secret": self.settings.withings_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.withings_redirect_uri,
            }
        )
        userid = str(body["userid"])
        if self.settings.withings_user_id and userid != self.settings.withings_user_id:
            raise WithingsError("authorized Withings user does not match WITHINGS_USER_ID")
        await self._store_tokens(userid, body)
        return userid

    async def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(4):
            try:
                response = await self.http.post(f"{WITHINGS_BASE_URL}/v2/oauth2", data=data)
            except (httpx.ConnectError, httpx.TimeoutException) as error:
                if attempt == 3:
                    raise WithingsError(
                        "Withings token endpoint is unavailable", retryable=True
                    ) from error
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise WithingsError(
                        "Withings token endpoint returned a transient error", retryable=True
                    )
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.status_code >= 400:
                raise WithingsError("Withings token request was rejected")

            payload = _safe_json(response, "Withings token endpoint returned invalid JSON")
            status = int(payload.get("status", -1))
            if status == 0 and isinstance(payload.get("body"), dict):
                return payload["body"]
            if status in (522, 601) and attempt < 3:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise WithingsError("Withings token request failed", status=status)
        raise AssertionError("unreachable")

    async def _store_tokens(self, userid: str, body: dict[str, Any]) -> None:
        now = int(time.time())
        expires_at = now + int(body["expires_in"])
        async with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO oauth_tokens(
                    userid, access_token, refresh_token, expires_at, scope, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(userid) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scope = excluded.scope,
                    updated_at = excluded.updated_at
                """,
                (
                    userid,
                    str(body["access_token"]),
                    str(body["refresh_token"]),
                    expires_at,
                    str(body.get("scope", "")),
                    now,
                ),
            )

    async def resolve_userid(self) -> str:
        if self.settings.withings_user_id:
            return self.settings.withings_user_id
        async with self.database.read() as connection:
            rows = connection.execute(
                "SELECT userid FROM oauth_tokens ORDER BY updated_at DESC LIMIT 2"
            ).fetchall()
        if not rows:
            raise WithingsError("Withings OAuth is not connected")
        if len(rows) > 1:
            raise WithingsError("WITHINGS_USER_ID is required when multiple users are stored")
        return str(rows[0]["userid"])

    async def access_token(self, userid: str, *, force_refresh: bool = False) -> str:
        async with self._refresh_lock:
            async with self.database.read() as connection:
                token = connection.execute(
                    "SELECT * FROM oauth_tokens WHERE userid = ?", (userid,)
                ).fetchone()
            if token is None:
                raise WithingsError("no OAuth token stored for Withings user")
            if not force_refresh and int(token["expires_at"]) > int(time.time()) + 90:
                return str(token["access_token"])

            body = await self._token_request(
                {
                    "action": "requesttoken",
                    "client_id": self.settings.withings_client_id,
                    "client_secret": self.settings.withings_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": str(token["refresh_token"]),
                }
            )
            response_userid = str(body.get("userid", userid))
            if response_userid != userid:
                raise WithingsError("refreshed token belongs to a different Withings user")
            await self._store_tokens(userid, body)
            return str(body["access_token"])

    async def _authorized_post(
        self, userid: str, path: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        refreshed = False
        for attempt in range(4):
            token = await self.access_token(userid, force_refresh=refreshed)
            refreshed = False
            try:
                response = await self.http.post(
                    f"{WITHINGS_BASE_URL}{path}",
                    data=data,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except (httpx.ConnectError, httpx.TimeoutException) as error:
                if attempt == 3:
                    raise WithingsError("Withings API is unavailable", retryable=True) from error
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code == 401:
                if attempt == 3:
                    raise WithingsError("Withings access token was rejected")
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise WithingsError("Withings API returned a transient error", retryable=True)
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.status_code >= 400:
                raise WithingsError("Withings API request was rejected")

            payload = _safe_json(response, "Withings API returned invalid JSON")
            status = int(payload.get("status", -1))
            if status == 0 and isinstance(payload.get("body"), dict):
                return payload["body"]
            if status == 343 and attempt < 3:
                refreshed = True
                continue
            if status in (522, 601) and attempt < 3:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise WithingsError("Withings API request failed", status=status)
        raise AssertionError("unreachable")

    async def fetch_measurements(
        self,
        userid: str,
        *,
        startdate: int | None = None,
        enddate: int | None = None,
        lastupdate: int | None = None,
    ) -> FetchResult:
        base_data: dict[str, Any] = {
            "action": "getmeas",
            "meastypes": "9,10,11",
            "category": 1,
        }
        if lastupdate is not None:
            base_data["lastupdate"] = lastupdate
        else:
            if startdate is not None:
                base_data["startdate"] = startdate
            if enddate is not None:
                base_data["enddate"] = enddate

        groups: list[dict[str, Any]] = []
        offset: int | None = None
        updatetime: int | None = None
        seen_offsets: set[int] = set()
        for _ in range(100):
            data = dict(base_data)
            if offset is not None:
                data["offset"] = offset
            body = await self._authorized_post(userid, "/measure", data)
            page_groups = body.get("measuregrps") or []
            if isinstance(page_groups, list):
                groups.extend(group for group in page_groups if isinstance(group, dict))
            if body.get("updatetime") is not None:
                updatetime = int(body["updatetime"])
            if not body.get("more"):
                break
            next_offset = int(body["offset"])
            if next_offset in seen_offsets:
                raise WithingsError("Withings pagination returned a repeated offset")
            seen_offsets.add(next_offset)
            offset = next_offset
        else:
            raise WithingsError("Withings pagination exceeded 100 pages")
        return FetchResult(groups=groups, updatetime=updatetime)

    async def subscribe_to_blood_pressure(self, userid: str) -> None:
        await self._authorized_post(
            userid,
            "/notify",
            {
                "action": "subscribe",
                "callbackurl": self.settings.webhook_callback_url,
                "appli": self.settings.withings_bp_appli,
                "comment": "BP Reporter",
            },
        )

    async def list_subscriptions(self, userid: str) -> list[dict[str, Any]]:
        body = await self._authorized_post(
            userid,
            "/notify",
            {"action": "list", "appli": self.settings.withings_bp_appli},
        )
        profiles = body.get("profiles") or []
        return [profile for profile in profiles if isinstance(profile, dict)]


class TelegramClient:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.http = http_client or httpx.AsyncClient(timeout=35)
        self._owns_http = http_client is None

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if not self.settings.telegram_bot_token:
            raise TelegramDefinitiveError("Telegram bot token is not configured")
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        try:
            response = await self.http.post(url, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise TelegramDefinitiveError("Telegram connection failed") from error
        except httpx.HTTPError as error:
            raise TelegramAmbiguousError("Telegram request outcome is unknown") from error

        if response.status_code >= 500:
            raise TelegramAmbiguousError("Telegram request outcome is unknown")
        data = _safe_json(response, "Telegram returned invalid JSON")
        if response.status_code == 429 or (
            isinstance(data, dict) and int(data.get("error_code", 0)) == 429
        ):
            parameters = data.get("parameters") if isinstance(data, dict) else None
            retry_after = int(parameters.get("retry_after", 1)) if parameters else 1
            raise TelegramDefinitiveError("Telegram rate limit", retry_after=retry_after)
        if response.status_code >= 400 or not isinstance(data, dict) or not data.get("ok"):
            raise TelegramDefinitiveError("Telegram rejected the request")
        return data.get("result")

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        silent: bool | None = None,
    ) -> SentMessage:
        result = await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_notification": self.settings.telegram_silent if silent is None else silent,
            },
        )
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramAmbiguousError("Telegram response did not contain message_id")
        result_chat = result.get("chat") or {}
        return SentMessage(
            message_id=int(result["message_id"]),
            chat_id=result_chat.get("id", chat_id),
        )

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 25,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramDefinitiveError("Telegram getUpdates returned an invalid result")
        return [update for update in result if isinstance(update, dict)]

    async def get_me(self) -> dict[str, Any]:
        result = await self.call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramDefinitiveError("Telegram getMe returned an invalid result")
        return result


def _safe_json(response: httpx.Response, message: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        if "Telegram" in message:
            raise TelegramDefinitiveError(message) from error
        raise WithingsError(message) from error
    if not isinstance(payload, dict):
        if "Telegram" in message:
            raise TelegramDefinitiveError(message)
        raise WithingsError(message)
    return payload
