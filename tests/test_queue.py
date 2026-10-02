import json
from collections.abc import Iterable
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID

from app.queue.pipeline_runner import (
    PipelineRunner,
    PipelineStep,
    RunResult,
    StepOutcome,
)
from app.queue.postgres import PostgresQueue, QueueItem
from app.queue.state_machine import JobStatus
from app.queue.worker import Worker


class FakeQueue:
    def __init__(self, item: QueueItem | None) -> None:
        self.item = item
        self.succeeded: list[int] = []
        self.retried: list[tuple[int, str, int]] = []

    def claim(self, worker_id: str) -> QueueItem | None:
        return self.item

    def succeed(self, item: QueueItem, worker_id: str) -> None:
        self.succeeded.append(item.id)

    def retry_or_fail(
        self,
        item: QueueItem,
        worker_id: str,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> None:
        self.retried.append((item.id, error, max_attempts))


class RecordingQueueConnection:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row
        self.queries: list[str] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]:
        self.queries.append(query)
        if "RETURNING" in query:
            return [self.row]
        return []


class FakePipelineStore:
    def __init__(self, status: JobStatus = JobStatus.FOUND) -> None:
        self.status = status
        self.lock_available = True
        self.lock_released = False
        self.finished_runs: set[tuple[UUID, int]] = set()
        self.logs: list[dict[str, object]] = []

    def try_acquire_lock(self) -> bool:
        return self.lock_available

    def release_lock(self) -> None:
        self.lock_released = True

    def is_finished(self, run_id: UUID, job_id: int) -> bool:
        return (run_id, job_id) in self.finished_runs

    def get_status(self, job_id: int) -> JobStatus:
        return self.status

    def set_status(
        self, job_id: int, expected: JobStatus, target: JobStatus
    ) -> None:
        assert self.status == expected
        self.status = target

    def write_log(self, **values: object) -> None:
        self.logs.append(values)
        if values["step"] == "pipeline" and values["status"] in {
            "completed",
            "failed",
        }:
            self.finished_runs.add((values["run_id"], values["job_id"]))


def _load_queue_item(project_root: Path) -> QueueItem:
    values = json.loads(
        (project_root / "tests/fixtures/queue_job.json").read_text(encoding="utf-8")
    )
    return QueueItem(
        id=values["id"],
        run_id=UUID(values["run_id"]),
        job_id=values["job_id"],
        task=values["task"],
        payload=values["payload"],
        attempts=values["attempts"],
    )


def test_worker_completes_a_claimed_item(project_root: Path) -> None:
    item = _load_queue_item(project_root)
    queue = FakeQueue(item)
    handled: list[int] = []
    worker = Worker(queue, "worker-1", {"pipeline": lambda value: handled.append(value.id)})

    assert worker.run_once() is True
    assert handled == [item.id]
    assert queue.succeeded == [item.id]
    assert queue.retried == []


def test_postgres_queue_uses_idempotent_enqueue_and_skip_locked_claim(
    project_root: Path,
) -> None:
    item = _load_queue_item(project_root)
    row = (
        item.id,
        item.run_id,
        item.job_id,
        item.task,
        json.dumps(item.payload),
        item.attempts,
    )
    connection = RecordingQueueConnection(row)
    queue = PostgresQueue(connection)

    enqueued = queue.enqueue(
        idempotency_key="fixture-key",
        run_id=item.run_id,
        task=item.task,
        job_id=item.job_id,
        payload=item.payload,
    )
    claimed = queue.claim("worker-1")

    assert enqueued is not None and enqueued.id == item.id
    assert claimed == item
    assert any("ON CONFLICT (idempotency_key) DO NOTHING" in q for q in connection.queries)
    assert any("FOR UPDATE SKIP LOCKED" in q for q in connection.queries)


def test_worker_contains_task_failure(project_root: Path) -> None:
    item = _load_queue_item(project_root)
    queue = FakeQueue(item)

    def fail(_: QueueItem) -> None:
        raise RuntimeError("fixture failure")

    worker = Worker(queue, "worker-1", {"pipeline": fail})

    assert worker.run_once() is True
    assert queue.succeeded == []
    assert queue.retried == [(item.id, "fixture failure", 3)]


def test_pipeline_runner_advances_in_order_and_does_not_repeat() -> None:
    store = FakePipelineStore()
    calls: list[str] = []
    steps = (
        PipelineStep(
            "score",
            frozenset({JobStatus.FOUND}),
            lambda _: calls.append("score") or StepOutcome(JobStatus.SCORED),
        ),
        PipelineStep(
            "tailor",
            frozenset({JobStatus.SCORED}),
            lambda _: calls.append("tailor") or StepOutcome(JobStatus.TAILORED),
        ),
    )
    runner = PipelineRunner(store, steps)
    run_id = UUID("cd2b2163-9c35-48c8-a195-e66c9389c01f")

    assert runner.run(run_id=run_id, job_id=17, trigger="manual") == RunResult.COMPLETED
    assert runner.run(run_id=run_id, job_id=17, trigger="manual") == RunResult.ALREADY_FINISHED
    assert calls == ["score", "tailor"]
    assert store.status == JobStatus.TAILORED
    assert store.lock_released is True


def test_pipeline_runner_refuses_overlapping_run() -> None:
    store = FakePipelineStore()
    store.lock_available = False
    runner = PipelineRunner(store, ())

    result = runner.run(
        run_id=UUID("cd2b2163-9c35-48c8-a195-e66c9389c01f"),
        job_id=17,
        trigger="schedule",
    )

    assert result == RunResult.ALREADY_RUNNING
    assert store.logs == []


def test_pipeline_runner_marks_handler_failure_without_raising() -> None:
    store = FakePipelineStore()

    def fail(_: int) -> StepOutcome:
        raise RuntimeError("scorer unavailable")

    runner = PipelineRunner(
        store,
        (PipelineStep("score", frozenset({JobStatus.FOUND}), fail),),
    )

    result = runner.run(
        run_id=UUID("cd2b2163-9c35-48c8-a195-e66c9389c01f"),
        job_id=17,
        trigger="event",
    )

    assert result == RunResult.FAILED
    assert store.status == JobStatus.FAILED
    assert [log["status"] for log in store.logs] == ["failed", "failed"]
