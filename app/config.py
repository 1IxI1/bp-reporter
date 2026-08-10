from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    withings_client_id: str = ""
    withings_client_secret: str = ""
    withings_redirect_uri: str = ""
    withings_webhook_url: str = ""
    withings_webhook_secret: str = ""
    withings_user_id: str = ""
    withings_bp_appli: int = 4

    telegram_bot_token: str = ""
    telegram_owner_user_id: int | None = None
    telegram_private_chat_id: int | None = None
    telegram_channel_id: int | None = None
    telegram_silent: bool = True

    database_url: str = "sqlite:///./data/bp-reporter.db"
    app_secret: str = ""
    timezone: str = "UTC"
    live_after: str = ""
    session_timeout_minutes: int = 20
    polling_enabled: bool = False
    polling_interval_seconds: int = 10

    measurement_max_age_seconds: int = 600
    auto_pair_enabled: bool = True
    auto_pair_window_seconds: int = 3600
    initial_poll_lookback_seconds: int = 3600
    worker_interval_seconds: float = 1.0
    log_level: str = "INFO"
    log_medical_data: bool = False
    enable_docs: bool = False

    @field_validator("polling_interval_seconds", "worker_interval_seconds")
    @classmethod
    def positive_intervals(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("intervals must be positive")
        return value

    @field_validator("session_timeout_minutes", "measurement_max_age_seconds")
    @classmethod
    def positive_timeouts(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeouts must be positive")
        return value

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("DATABASE_URL must use sqlite:/// for this deployment")
        value = self.database_url[len(prefix) :]
        if not value:
            raise ValueError("DATABASE_URL does not contain a database path")
        return Path(value).expanduser().resolve()

    @property
    def display_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def live_after_utc(self) -> datetime | None:
        value = self.live_after.strip()
        if not value:
            return None
        if value.lstrip("-").isdigit():
            return datetime.fromtimestamp(int(value), tz=UTC)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("LIVE_AFTER must include a timezone or be a UTC timestamp")
        return parsed.astimezone(UTC)

    @property
    def webhook_callback_url(self) -> str:
        if not self.withings_webhook_url or not self.withings_webhook_secret:
            return self.withings_webhook_url
        parts = urlsplit(self.withings_webhook_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["token"] = self.withings_webhook_secret
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    def readiness_problems(self) -> list[str]:
        required = {
            "WITHINGS_CLIENT_ID": self.withings_client_id,
            "WITHINGS_CLIENT_SECRET": self.withings_client_secret,
            "WITHINGS_REDIRECT_URI": self.withings_redirect_uri,
            "WITHINGS_WEBHOOK_URL": self.withings_webhook_url,
            "WITHINGS_WEBHOOK_SECRET": self.withings_webhook_secret,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_OWNER_USER_ID": self.telegram_owner_user_id,
            "TELEGRAM_CHANNEL_ID": self.telegram_channel_id,
            "APP_SECRET": self.app_secret,
            "LIVE_AFTER": self.live_after,
        }
        return [name for name, value in required.items() if value in (None, "")]


def make_test_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "app_secret": "test-admin-secret",
        "withings_webhook_secret": "test-webhook-secret",
        "live_after": "2026-01-01T00:00:00Z",
        "telegram_owner_user_id": 100,
        "telegram_private_chat_id": 100,
        "telegram_channel_id": -100200,
        "timezone": "UTC",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
