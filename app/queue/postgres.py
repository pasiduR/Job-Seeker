"""PostgreSQL-backed queue operations with atomic claiming and retries."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class QueueConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


@dataclass(frozen=True)
class QueueItem:
    id: int
    run_id: UUID
    job_id: int | None
    task: str
    payload: Mapping[str, object]
    attempts: int


def _queue_item(row: tuple[Any, ...]) -> QueueItem:
    raw_payload = row[4]
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    return QueueItem(
        id=int(row[0]),
        run_id=UUID(str(row[1])),
        job_id=int(row[2]) if row[2] is not None else None,
        task=str(row[3]),
        payload=payload,
        attempts=int(row[5]),
    )


class PostgresQueue:
    def __init__(self, connection: QueueConnection) -> None:
        self._connection = connection

    def enqueue(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        task: str,
        job_id: int | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> QueueItem | None:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                INSERT INTO queue_jobs (
                    idempotency_key, run_id, job_id, task, payload
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id, run_id, job_id, task, payload, attempts
                """,
                (
                    idempotency_key,
                    run_id,
                    job_id,
                    task,
                    json.dumps(payload or {}),
                ),
            )
            row = next(iter(rows), None)
        return _queue_item(row) if row is not None else None

    def has_pending(
        self, *, task: str, job_id: int | None, payload: Mapping[str, object]
    ) -> bool:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                SELECT 1 FROM queue_jobs
                WHERE task = %s AND job_id IS NOT DISTINCT FROM %s::bigint
                  AND payload = %s::jsonb AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (task, job_id, json.dumps(payload)),
            )
            return next(iter(rows), None) is not None

    def claim(self, worker_id: str) -> QueueItem | None:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                WITH claimable AS (
                    SELECT id
                    FROM queue_jobs
                    WHERE status = 'queued' AND available_at <= now()
                    ORDER BY available_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE queue_jobs AS queue
                SET status = 'running',
                    attempts = queue.attempts + 1,
                    locked_at = now(),
                    locked_by = %s
                FROM claimable
                WHERE queue.id = claimable.id
                RETURNING queue.id, queue.run_id, queue.job_id, queue.task,
                          queue.payload, queue.attempts
                """,
                (worker_id,),
            )
            row = next(iter(rows), None)
        return _queue_item(row) if row is not None else None

    def succeed(self, item: QueueItem, worker_id: str) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                UPDATE queue_jobs
                SET status = 'succeeded', completed_at = now(),
                    locked_at = NULL, locked_by = NULL, last_error = NULL
                WHERE id = %s AND status = 'running' AND locked_by = %s
                """,
                (item.id, worker_id),
            )

    def defer(self, item: QueueItem, worker_id: str) -> None:
        with self._connection.transaction():
            self._connection.execute("""
                UPDATE queue_jobs SET status = 'queued',
                    available_at = now() + interval '5 seconds',
                    attempts = greatest(attempts - 1, 0), locked_at = NULL, locked_by = NULL
                WHERE id = %s AND status = 'running' AND locked_by = %s
            """, (item.id, worker_id))

    def retry_or_fail(
        self,
        item: QueueItem,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> None:
        retry = item.attempts < max_attempts
        delay_seconds = 2**item.attempts
        with self._connection.transaction():
            if retry:
                self._connection.execute(
                    """
                    UPDATE queue_jobs
                    SET status = 'queued',
                        available_at = now() + (%s * interval '1 second'),
                        locked_at = NULL, locked_by = NULL, last_error = %s
                    WHERE id = %s AND status = 'running' AND locked_by = %s
                    """,
                    (delay_seconds, error, item.id, worker_id),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE queue_jobs
                    SET status = 'failed', completed_at = now(),
                        locked_at = NULL, locked_by = NULL, last_error = %s
                    WHERE id = %s AND status = 'running' AND locked_by = %s
                    """,
                    (error, item.id, worker_id),
                )
