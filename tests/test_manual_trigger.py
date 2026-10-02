from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any
from uuid import UUID

import pytest

from app.queue.postgres import PostgresQueue, QueueItem
from app.triggers.manual import RUN_JOB_TASK, RUN_PIPELINE_TASK, ManualTrigger


RUN_ID = UUID("4a1d6c1e-2b8f-4f7e-9a51-6b3c2d1e0f9a")


class MemoryTriggerQueue:
    """Keeps queued items pending so repeated clicks can be detected."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def enqueue(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        task: str,
        job_id: int | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> QueueItem | None:
        if any(item["idempotency_key"] == idempotency_key for item in self.items):
            return None
        item = {
            "idempotency_key": idempotency_key,
            "run_id": run_id,
            "task": task,
            "job_id": job_id,
            "payload": dict(payload or {}),
        }
        self.items.append(item)
        return QueueItem(len(self.items), run_id, job_id, task, item["payload"], 0)

    def has_pending(
        self, *, task: str, job_id: int | None, payload: Mapping[str, object]
    ) -> bool:
        return any(
            (item["task"], item["job_id"], item["payload"]) == (task, job_id, dict(payload))
            for item in self.items
        )


def test_run_now_queues_all_steps_with_manual_trigger() -> None:
    queue = MemoryTriggerQueue()
    result = ManualTrigger(queue, new_run_id=lambda: RUN_ID).run_now()

    assert result.queued is True
    assert result.run_id == RUN_ID
    assert queue.items == [
        {
            "idempotency_key": f"manual:{RUN_PIPELINE_TASK}:-:{RUN_ID}",
            "run_id": RUN_ID,
            "task": RUN_PIPELINE_TASK,
            "job_id": None,
            "payload": {
                "steps": ["find_sources", "scrape", "score", "tailor", "fill"],
                "trigger": "manual",
            },
        }
    ]


def test_single_step_and_job_runs_do_not_double_queue() -> None:
    queue = MemoryTriggerQueue()
    trigger = ManualTrigger(queue)

    assert trigger.run_now("tailor").queued is True
    assert trigger.run_now("tailor").queued is False
    assert trigger.run_now("score").queued is True
    assert trigger.run_for_job(7).queued is True
    repeat = trigger.run_for_job(7)

    assert repeat.queued is False
    assert "already queued" in repeat.message
    assert [(item["task"], item["job_id"]) for item in queue.items] == [
        (RUN_PIPELINE_TASK, None),
        (RUN_PIPELINE_TASK, None),
        (RUN_JOB_TASK, 7),
    ]


def test_unknown_step_is_rejected() -> None:
    with pytest.raises(ValueError):
        ManualTrigger(MemoryTriggerQueue()).run_now("submit")


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        self.statements.append((" ".join(query.split()), params))
        return [(1,)]


def test_postgres_pending_check_matches_task_job_and_payload() -> None:
    connection = RecordingConnection()

    pending = PostgresQueue(connection).has_pending(
        task=RUN_JOB_TASK, job_id=None, payload={"trigger": "manual"}
    )

    query, params = connection.statements[0]
    assert pending is True
    assert "job_id IS NOT DISTINCT FROM %s::bigint" in query
    assert "status IN ('queued', 'running')" in query
    assert params == (RUN_JOB_TASK, None, '{"trigger": "manual"}')
