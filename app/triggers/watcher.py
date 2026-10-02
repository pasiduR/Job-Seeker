"""Enqueue due polls; the existing worker fetches sources and starts new jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.config import RuntimeSettings
from app.queue.postgres import PostgresQueue, QueueConnection, QueueItem
from app.sources.dispatch import SourceDispatcher
from app.sources.models import SourceRepository
from app.sources.scraper import PostgresJobStore, SearchFilter, canonicalize_job_url, matches_filter
from app.triggers.manual import RUN_JOB_TASK


POLL_SUBSCRIPTION_TASK = "poll_subscription"


def enqueue_due(connection: QueueConnection, *, now: datetime, settings: RuntimeSettings) -> int:
    if now.tzinfo is None:
        raise ValueError("Watcher time must be timezone-aware")
    count = 0
    with connection.transaction():
        rows = list(connection.execute("""
            SELECT sub.id, sub.polling_interval_minutes, sub.last_enqueued_at, s.type, s.config
            FROM subscriptions sub JOIN sources s ON s.id = sub.source_id
            JOIN search_filters f ON f.id = sub.search_filter_id
            WHERE sub.active AND s.active AND f.active
            ORDER BY sub.id FOR UPDATE OF sub SKIP LOCKED
        """))
        queue = PostgresQueue(connection)
        for sub_id, interval, last, source_type, config in rows:
            if source_type == "ats_board":
                interval = max(interval, settings.watcher_ats_min_minutes)
            elif source_type == "job_board" and config.get("adapter") == "jobspy":
                interval = max(interval, settings.watcher_jobspy_min_minutes)
            if last is not None and now < last + timedelta(minutes=interval):
                continue
            payload = {"trigger": "event", "subscription_id": sub_id}
            if queue.has_pending(task=POLL_SUBSCRIPTION_TASK, job_id=None, payload=payload):
                continue
            item = queue.enqueue(idempotency_key=f"poll:{sub_id}:{now.isoformat()}",
                                 run_id=uuid4(), task=POLL_SUBSCRIPTION_TASK, payload=payload)
            connection.execute("UPDATE subscriptions SET last_enqueued_at = %s WHERE id = %s", (now, sub_id))
            count += item is not None
    return count


class Watcher:
    def __init__(self, connection: QueueConnection, dispatcher: SourceDispatcher) -> None:
        self._connection = connection
        self._dispatcher = dispatcher

    def poll(self, item: QueueItem) -> str | None:
        sub_id = int(item.payload["subscription_id"])
        with self._connection.transaction():
            rows = list(self._connection.execute("""
                SELECT sub.source_id, f.roles, f.locations, f.remote, f.exclude_keywords
                FROM subscriptions sub JOIN sources s ON s.id = sub.source_id
                JOIN search_filters f ON f.id = sub.search_filter_id
                WHERE sub.id = %s AND sub.active AND s.active AND f.active
            """, (sub_id,)))
        if not rows:
            return None
        source_id, roles, locations, remote, excluded = rows[0]
        source = SourceRepository(self._connection).get(source_id)
        if source is None:
            return None
        search = SearchFilter(roles=roles or (), locations=locations or (), remote=remote,
                              exclude_keywords=excluded or ())
        listings = self._dispatcher.fetch(source, search, hours_old=1)
        with self._connection.transaction():
            # Recheck after fetching: disabling/removing the subscription cancels saving.
            active = list(self._connection.execute("""
                SELECT sub.id FROM subscriptions sub JOIN sources s ON s.id = sub.source_id
                JOIN search_filters f ON f.id = sub.search_filter_id
                WHERE sub.id = %s AND sub.active AND s.active AND f.active FOR UPDATE OF sub
            """, (sub_id,)))
            if not active:
                return None
            store = PostgresJobStore(self._connection)
            queue = PostgresQueue(self._connection)
            for listing in listings:
                if not matches_filter(listing, search):
                    continue
                listing = listing.model_copy(update={"url": canonicalize_job_url(listing.url)})
                job_id = store.save_found_id(source_id, listing)
                if job_id is not None:
                    self._connection.execute("UPDATE jobs SET application_lane = 'fast_lane' WHERE id = %s", (job_id,))
                    queue.enqueue(idempotency_key=f"event:job:{job_id}", run_id=uuid4(),
                                  task=RUN_JOB_TASK, job_id=job_id,
                                  payload={"trigger": "event", "lane": "fast_lane", "subscription_id": sub_id})
            self._connection.execute("UPDATE subscriptions SET last_polled_at = now() WHERE id = %s", (sub_id,))
        return None


def main() -> None:
    import argparse
    from app.config import SecretSettings, read_settings_table
    from app.db.connection import open_pool

    argparse.ArgumentParser(description=__doc__).parse_args()
    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    with open_pool(secrets.database_url.get_secret_value()) as pool, pool.connection() as connection:
        settings = RuntimeSettings.model_validate(read_settings_table(connection))
        enqueue_due(connection, now=datetime.now(timezone.utc), settings=settings)


if __name__ == "__main__":
    main()
