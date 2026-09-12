from __future__ import annotations

import asyncio
import logging

from app.clients import TelegramError, WithingsError
from app.service import BPService

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, service: BPService) -> None:
        self.service = service
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._webhook_loop(), name="webhook-worker"),
            asyncio.create_task(self._outbox_loop(), name="telegram-outbox-worker"),
            asyncio.create_task(self._maintenance_loop(), name="session-maintenance"),
        ]
        if self.service.settings.polling_enabled:
            self.tasks.append(asyncio.create_task(self._poll_loop(), name="withings-poller"))
        if self.service.settings.telegram_bot_token:
            self.tasks.append(asyncio.create_task(self._telegram_loop(), name="telegram-updates"))
            self.tasks.append(
                asyncio.create_task(self._telegram_commands_loop(), name="telegram-command-menu")
            )

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _webhook_loop(self) -> None:
        while True:
            try:
                worked = await self.service.process_webhook_once()
            except Exception:
                logger.exception("webhook_worker_error")
                worked = False
            if not worked:
                await asyncio.sleep(self.service.settings.worker_interval_seconds)

    async def _outbox_loop(self) -> None:
        while True:
            try:
                worked = await self.service.process_outbox_once()
            except Exception:
                logger.exception("outbox_worker_error")
                worked = False
            if not worked:
                await asyncio.sleep(self.service.settings.worker_interval_seconds)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                expired = await self.service.expire_sessions()
                if expired:
                    logger.info("sessions_expired", extra={"count": expired})
            except Exception:
                logger.exception("session_maintenance_error")
            await asyncio.sleep(min(10, self.service.settings.worker_interval_seconds * 5))

    async def _poll_loop(self) -> None:
        while True:
            if await self.service.oauth_connected():
                try:
                    stats = await self.service.poll_once()
                    logger.info("withings_poll_succeeded", extra=stats)
                except WithingsError as error:
                    status = error.status if error.status is not None else -1
                    await self.service.set_state("poll_last_status", str(status))
                    await self.service.set_state("poll_last_error_at", str(self.service.now()))
                    logger.warning("withings_poll_failed", extra={"status_code": status})
                except Exception:
                    logger.exception("withings_poll_worker_error")
            await asyncio.sleep(self.service.settings.polling_interval_seconds)

    async def _telegram_loop(self) -> None:
        while True:
            value = await self.service.get_state("telegram_update_offset")
            offset = int(value) if value else None
            try:
                updates = await self.service.telegram.get_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if update_id is None:
                        continue
                    await self.service.process_telegram_update(update)
                    await self.service.set_state("telegram_update_offset", str(int(update_id) + 1))
            except TelegramError:
                logger.warning("telegram_updates_failed")
                await asyncio.sleep(5)
            except Exception:
                logger.exception("telegram_updates_worker_error")
                await asyncio.sleep(5)

    async def _telegram_commands_loop(self) -> None:
        while True:
            try:
                configured = await self.service.configure_telegram_commands()
                if configured:
                    logger.info("telegram_commands_configured")
                return
            except TelegramError:
                logger.warning("telegram_commands_configuration_failed")
                await asyncio.sleep(30)
            except Exception:
                logger.exception("telegram_commands_worker_error")
                await asyncio.sleep(30)
