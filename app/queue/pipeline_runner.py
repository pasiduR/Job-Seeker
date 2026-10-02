"""Deterministic pipeline orchestration and sole owner of job status writes."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from app.queue.state_machine import (
    JobStatus,
    mark_approved,
    mark_failed,
    mark_filled,
    mark_needs_manual,
    mark_scored,
    mark_skipped,
    mark_submitted,
    mark_tailored,
)


_PIPELINE_LOCK_ID = 1_906_151_933


class PipelineConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class ConcurrentStatusUpdate(RuntimeError):
    pass


class RunResult(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ALREADY_RUNNING = "already_running"
    ALREADY_FINISHED = "already_finished"


@dataclass(frozen=True)
class StepOutcome:
    status: JobStatus
    error: str | None = None


StepHandler = Callable[[int], StepOutcome]


@dataclass(frozen=True)
class PipelineStep:
    name: str
    eligible_statuses: frozenset[JobStatus]
    handler: StepHandler


class PipelineStore(Protocol):
    def try_acquire_lock(self) -> bool: ...

    def release_lock(self) -> None: ...

    def is_finished(self, run_id: UUID, job_id: int) -> bool: ...

    def get_status(self, job_id: int) -> JobStatus: ...

    def set_status(
        self, job_id: int, expected: JobStatus, target: JobStatus
    ) -> None: ...

    def write_log(
        self,
        *,
        run_id: UUID,
        trigger: str,
        step: str,
        job_id: int,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None: ...


class PostgresPipelineStore:
    def __init__(self, connection: PipelineConnection) -> None:
        self._connection = connection

    def try_acquire_lock(self) -> bool:
        with self._connection.transaction():
            rows = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (_PIPELINE_LOCK_ID,)
            )
            row = next(iter(rows))
        return bool(row[0])

    def release_lock(self) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "SELECT pg_advisory_unlock(%s)", (_PIPELINE_LOCK_ID,)
            )

    def is_finished(self, run_id: UUID, job_id: int) -> bool:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                SELECT 1 FROM run_logs
                WHERE run_id = %s AND job_id = %s AND step = 'pipeline'
                  AND status IN ('completed', 'failed')
                LIMIT 1
                """,
                (run_id, job_id),
            )
            return next(iter(rows), None) is not None

    def get_status(self, job_id: int) -> JobStatus:
        with self._connection.transaction():
            rows = self._connection.execute(
                "SELECT status FROM jobs WHERE id = %s", (job_id,)
            )
            row = next(iter(rows), None)
        if row is None:
            raise LookupError(f"Job {job_id} does not exist")
        return JobStatus(str(row[0]))

    def set_status(
        self, job_id: int, expected: JobStatus, target: JobStatus
    ) -> None:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                UPDATE jobs SET status = %s, updated_at = now()
                WHERE id = %s AND status = %s
                RETURNING id
                """,
                (target.value, job_id, expected.value),
            )
            if next(iter(rows), None) is None:
                raise ConcurrentStatusUpdate(
                    f"Job {job_id} no longer has status {expected.value}"
                )

    def write_log(
        self,
        *,
        run_id: UUID,
        trigger: str,
        step: str,
        job_id: int,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO run_logs (
                    run_id, trigger, step, job_id, status, duration_ms, error
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, trigger, step, job_id, status, duration_ms, error),
            )


_TRANSITIONS = {
    JobStatus.SCORED: mark_scored,
    JobStatus.TAILORED: mark_tailored,
    JobStatus.FILLED: mark_filled,
    JobStatus.APPROVED: mark_approved,
    JobStatus.SUBMITTED: mark_submitted,
    JobStatus.SKIPPED: mark_skipped,
    JobStatus.FAILED: mark_failed,
    JobStatus.NEEDS_MANUAL: mark_needs_manual,
}


class PipelineRunner:
    def __init__(self, store: PipelineStore, steps: Sequence[PipelineStep]) -> None:
        self._store = store
        self._steps = steps

    def run(self, *, run_id: UUID, job_id: int, trigger: str) -> RunResult:
        if not self._store.try_acquire_lock():
            return RunResult.ALREADY_RUNNING

        pipeline_started = perf_counter()
        try:
            if self._store.is_finished(run_id, job_id):
                return RunResult.ALREADY_FINISHED

            for step in self._steps:
                current = self._store.get_status(job_id)
                if current not in step.eligible_statuses:
                    continue
                step_started = perf_counter()
                try:
                    outcome = step.handler(job_id)
                    target = _TRANSITIONS[outcome.status](current)
                    self._store.set_status(job_id, current, target)
                except Exception as exc:
                    error = str(exc)
                    failed = mark_failed(current)
                    self._store.set_status(job_id, current, failed)
                    self._store.write_log(
                        run_id=run_id,
                        trigger=trigger,
                        step=step.name,
                        job_id=job_id,
                        status="failed",
                        duration_ms=_elapsed_ms(step_started),
                        error=error,
                    )
                    self._write_pipeline_log(
                        run_id, trigger, job_id, "failed", pipeline_started, error
                    )
                    return RunResult.FAILED

                self._store.write_log(
                    run_id=run_id,
                    trigger=trigger,
                    step=step.name,
                    job_id=job_id,
                    status=target.value,
                    duration_ms=_elapsed_ms(step_started),
                    error=outcome.error,
                )
                if target == JobStatus.FAILED:
                    self._write_pipeline_log(
                        run_id,
                        trigger,
                        job_id,
                        "failed",
                        pipeline_started,
                        outcome.error,
                    )
                    return RunResult.FAILED
                if target in {
                    JobStatus.SKIPPED,
                    JobStatus.NEEDS_MANUAL,
                    JobStatus.SUBMITTED,
                }:
                    break

            self._write_pipeline_log(
                run_id, trigger, job_id, "completed", pipeline_started, None
            )
            return RunResult.COMPLETED
        finally:
            self._store.release_lock()

    def _write_pipeline_log(
        self,
        run_id: UUID,
        trigger: str,
        job_id: int,
        status: str,
        started: float,
        error: str | None,
    ) -> None:
        self._store.write_log(
            run_id=run_id,
            trigger=trigger,
            step="pipeline",
            job_id=job_id,
            status=status,
            duration_ms=_elapsed_ms(started),
            error=error,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))
