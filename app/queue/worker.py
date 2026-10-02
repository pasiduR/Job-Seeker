"""Single-item queue worker that contains task failures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from app.queue.postgres import QueueItem


class QueueBackend(Protocol):
    def defer(self, item: QueueItem, worker_id: str) -> None: ...

    def claim(self, worker_id: str) -> QueueItem | None: ...

    def succeed(self, item: QueueItem, worker_id: str) -> None: ...

    def retry_or_fail(
        self,
        item: QueueItem,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> None: ...


TaskHandler = Callable[[QueueItem], None]


class RetryLater(RuntimeError):
    """Temporary contention: defer without consuming a failure attempt."""


class Worker:
    def __init__(
        self,
        queue: QueueBackend,
        worker_id: str,
        handlers: Mapping[str, TaskHandler],
        *,
        max_attempts: int = 3,
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._handlers = handlers
        self._max_attempts = max_attempts

    def run_once(self) -> bool:
        """Process one available item; return false when the queue is empty."""

        item = self._queue.claim(self._worker_id)
        if item is None:
            return False

        try:
            handler = self._handlers[item.task]
            handler(item)
        except RetryLater:
            self._queue.defer(item, self._worker_id)
        except Exception as exc:  # The worker must survive individual task failures.
            self._queue.retry_or_fail(
                item,
                self._worker_id,
                str(exc),
                max_attempts=self._max_attempts,
            )
        else:
            self._queue.succeed(item, self._worker_id)
        return True
