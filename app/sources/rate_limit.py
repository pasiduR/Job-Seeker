"""Database request reservations preserve source pacing across workers/restarts."""

from collections.abc import Callable, Mapping
from time import sleep

from app.queue.postgres import QueueConnection


class PostgresRateLimiter:
    def __init__(self, connection: QueueConnection, intervals: Mapping[str, float],
                 *, sleeper: Callable[[float], None] = sleep) -> None:
        self._connection = connection
        self._intervals = intervals
        self._sleep = sleeper

    def wait(self, source: str) -> None:
        interval = self._intervals.get(source, self._intervals.get(source.split(":", 1)[0], self._intervals["*"]))
        with self._connection.transaction():
            row = next(iter(self._connection.execute("""
                INSERT INTO request_pacing (source_key, next_allowed_at)
                VALUES (%s, now() + %s * interval '1 second')
                ON CONFLICT (source_key) DO UPDATE SET
                    next_allowed_at = greatest(now(), request_pacing.next_allowed_at)
                                      + %s * interval '1 second'
                RETURNING extract(epoch FROM (next_allowed_at - now())) - %s
            """, (source, interval, interval, interval))))
        delay = float(row[0])
        if delay > 0:
            self._sleep(delay)
