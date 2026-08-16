from __future__ import annotations

import hmac
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients import TelegramClient, TelegramError, WithingsClient, WithingsError
from app.config import Settings
from app.db import Database
from app.runtime import Runtime
from app.service import BPService


class JsonFormatter(logging.Formatter):
    _standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._standard and key not in {"args", "msg"}:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # httpx logs full request URLs; Telegram embeds the bot token in its URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(
    settings: Settings | None = None,
    *,
    withings_client: WithingsClient | None = None,
    telegram_client: TelegramClient | None = None,
    start_workers: bool = True,
) -> FastAPI:
    app_settings = settings or Settings()
    configure_logging(app_settings.log_level)
    database = Database(app_settings.database_path)
    withings = withings_client or WithingsClient(app_settings, database)
    telegram = telegram_client or TelegramClient(app_settings)
    service = BPService(app_settings, database, withings, telegram)
    runtime = Runtime(service)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await database.initialize()
        await service.recover_interrupted_outbox()
        if start_workers:
            runtime.start()
        yield
        if start_workers:
            await runtime.stop()
        await withings.close()
        await telegram.close()

    application = FastAPI(
        title="BP Reporter",
        docs_url="/docs" if app_settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if app_settings.enable_docs else None,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.database = database
    application.state.service = service

    async def require_admin(
        authorization: Annotated[str | None, Header()] = None,
        x_admin_secret: Annotated[str | None, Header()] = None,
    ) -> None:
        if not app_settings.app_secret:
            raise HTTPException(status_code=503, detail="APP_SECRET is not configured")
        bearer = ""
        if authorization and authorization.startswith("Bearer "):
            bearer = authorization.removeprefix("Bearer ")
        supplied = x_admin_secret or bearer
        if not supplied or not hmac.compare_digest(supplied, app_settings.app_secret):
            raise HTTPException(status_code=401, detail="unauthorized")

    def verify_webhook_token(request: Request) -> None:
        expected = app_settings.withings_webhook_secret
        supplied = request.query_params.get("token", "")
        if not expected:
            raise HTTPException(status_code=503, detail="webhook secret is not configured")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="invalid webhook token")

    @application.get("/health")
    async def health() -> dict[str, Any]:
        problems = app_settings.readiness_problems()
        connected = await service.oauth_connected()
        poll_status = await service.get_state("poll_last_status")
        return {
            "status": "ok",
            "ready": not problems and connected,
            "oauth_connected": connected,
            "configuration_missing": problems,
            "polling_enabled": app_settings.polling_enabled,
            "polling_interval_seconds": app_settings.polling_interval_seconds,
            "poll_last_status": poll_status,
        }

    @application.get("/oauth/start")
    async def oauth_start() -> RedirectResponse:
        if not (
            app_settings.withings_client_id
            and app_settings.withings_client_secret
            and app_settings.withings_redirect_uri
        ):
            raise HTTPException(status_code=503, detail="Withings OAuth is not configured")
        return RedirectResponse(await service.create_oauth_url(), status_code=302)

    @application.head("/oauth/callback", status_code=200)
    async def oauth_callback_head() -> Response:
        return Response(status_code=200)

    @application.get("/oauth/callback", response_class=HTMLResponse)
    async def oauth_callback(
        code: str | None = None,
        state_value: Annotated[str | None, Query(alias="state")] = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if error:
            raise HTTPException(status_code=400, detail="Withings authorization was denied")
        if not code or not state_value or not await service.consume_oauth_state(state_value):
            raise HTTPException(status_code=400, detail="invalid or expired OAuth callback")
        try:
            await withings.exchange_code(code)
        except WithingsError as api_error:
            raise HTTPException(status_code=502, detail=str(api_error)) from api_error
        return HTMLResponse(
            "<h1>Withings connected</h1><p>You can close this window.</p>", status_code=200
        )

    @application.head("/withings/webhook", status_code=200)
    async def withings_webhook_head(request: Request) -> Response:
        verify_webhook_token(request)
        return Response(status_code=200)

    @application.post("/withings/webhook", status_code=202)
    async def withings_webhook(request: Request) -> dict[str, bool]:
        verify_webhook_token(request)
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(status_code=415, detail="unsupported content type")
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            form = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=20)
            userid = form["userid"][0]
            appli = int(form["appli"][0])
            startdate = int(form["startdate"][0])
            enddate = int(form["enddate"][0])
            inserted = await service.enqueue_webhook(
                userid=userid, appli=appli, startdate=startdate, enddate=enddate
            )
        except (UnicodeDecodeError, KeyError, ValueError) as parse_error:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from parse_error
        return {"queued": inserted}

    @application.get("/debug/recent-measurements")
    async def recent_measurements(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        _: None = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return await service.recent_measurements(limit)

    @application.get("/debug/session")
    async def debug_session(_: None = Depends(require_admin)) -> dict[str, Any]:
        return await service.debug_session()

    @application.post("/admin/subscribe-webhook")
    async def subscribe_webhook(_: None = Depends(require_admin)) -> dict[str, Any]:
        if not app_settings.webhook_callback_url.startswith("https://"):
            raise HTTPException(status_code=503, detail="WITHINGS_WEBHOOK_URL must use HTTPS")
        try:
            userid = await withings.resolve_userid()
            subscriptions = await withings.list_subscriptions(userid)
            exists = any(
                str(profile.get("callbackurl")) == app_settings.webhook_callback_url
                and int(profile.get("appli", -1)) == app_settings.withings_bp_appli
                for profile in subscriptions
            )
            if not exists:
                await withings.subscribe_to_blood_pressure(userid)
        except WithingsError as api_error:
            raise HTTPException(status_code=502, detail=str(api_error)) from api_error
        return {
            "subscribed": True,
            "already_existed": exists,
            "appli": app_settings.withings_bp_appli,
        }

    @application.post("/admin/poll-now")
    async def poll_now(_: None = Depends(require_admin)) -> dict[str, int]:
        try:
            return await service.poll_once()
        except WithingsError as api_error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Withings request failed (status={api_error.status})",
            ) from api_error

    @application.post("/admin/test-telegram", status_code=202)
    async def test_telegram(
        target: Annotated[str, Query(pattern="^(private|channel)$")] = "private",
        _: None = Depends(require_admin),
    ) -> dict[str, bool]:
        try:
            await telegram.get_me()
            await service.enqueue_test_message(target)
        except (TelegramError, ValueError) as telegram_error:
            raise HTTPException(status_code=502, detail=str(telegram_error)) from telegram_error
        return {"queued": True}

    return application


app = create_app()
