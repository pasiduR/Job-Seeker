"""Run the notifier: ``python -m app.notify [--once]``.

Sends pending notifications and, with Telegram, long-polls for Approve /
Reject taps and queues them as review decisions for the worker.
"""

from __future__ import annotations

import argparse
import logging
from time import sleep

from app.config import RuntimeSettings, SecretSettings
from app.db.connection import open_pool
from app.http import HttpClient
from app.notify.channels import Channel, ChannelError, NtfyChannel, TelegramChannel
from app.notify.service import NotificationService, PostgresNotificationStore
from app.queue.postgres import PostgresQueue
from app.queue.tasks import PostgresWorkerStore
from app.triggers.review import ReviewDecisions


TELEGRAM_POLL_SECONDS = 25
IDLE_SECONDS = 10.0
ERROR_BACKOFF_SECONDS = 5.0

logger = logging.getLogger(__name__)


def build_channel(secrets: SecretSettings) -> Channel | None:
    http = HttpClient(
        timeout_seconds=TELEGRAM_POLL_SECONDS + 15,
        source_minimum_intervals={"telegram": 0.1, "ntfy": 1.0},
    )
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        return TelegramChannel(
            http,
            token=secrets.telegram_bot_token.get_secret_value(),
            chat_id=secrets.telegram_chat_id.get_secret_value(),
        )
    if secrets.ntfy_url:
        token = secrets.ntfy_token.get_secret_value() if secrets.ntfy_token else None
        return NtfyChannel(http, url=secrets.ntfy_url, token=token)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="send pending and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    channel = build_channel(secrets)
    if channel is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or NTFY_URL in .env")
    pool = open_pool(secrets.database_url.get_secret_value())
    offset: int | None = None

    def service(connection: object) -> NotificationService:
        settings = RuntimeSettings.model_validate(
            PostgresWorkerStore(connection).read_settings()  # type: ignore[arg-type]
        )
        return NotificationService(
            store=PostgresNotificationStore(connection),  # type: ignore[arg-type]
            channel=channel,
            decisions=ReviewDecisions(PostgresQueue(connection)),  # type: ignore[arg-type]
            dashboard_url=settings.dashboard_url,
        )

    while True:
        with pool.connection() as connection:
            service(connection).send_pending()
        if args.once:
            return
        if not isinstance(channel, TelegramChannel):
            sleep(IDLE_SECONDS)
            continue
        try:
            offset, taps = channel.poll(offset=offset, timeout_seconds=TELEGRAM_POLL_SECONDS)
        except ChannelError as exc:
            logger.warning("%s", exc)
            sleep(ERROR_BACKOFF_SECONDS)
            continue
        if taps:
            with pool.connection() as connection:
                service(connection).handle_taps(taps, channel)


if __name__ == "__main__":
    main()
