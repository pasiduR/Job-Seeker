"""Manual triggers: enqueue dashboard "Run now" requests for the worker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from app.queue.postgres import QueueItem


RUN_PIPELINE_TASK = "run_pipeline"
RUN_JOB_TASK = "run_job"
PIPELINE_STEPS = ("find_sources", "scrape", "score", "tailor", "fill")


class TriggerQueue(Protocol):
    def enqueue(
        self,
        *,
        idempotency_key: str,
        run_id: UUID,
        task: str,
        job_id: int | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> QueueItem | None: ...

    def has_pending(
        self, *, task: str, job_id: int | None, payload: Mapping[str, object]
    ) -> bool: ...


@dataclass(frozen=True)
class TriggerResult:
    queued: bool
    run_id: UUID | None
    message: str


class ManualTrigger:
    def __init__(
        self, queue: TriggerQueue, *, new_run_id: Callable[[], UUID] = uuid4
    ) -> None:
        self._queue = queue
        self._new_run_id = new_run_id

    def run_now(self, step: str | None = None) -> TriggerResult:
        """Queue the full pipeline, or a single step when ``step`` is given."""

        if step is not None and step not in PIPELINE_STEPS:
            raise ValueError(f"Unknown pipeline step: {step!r}")
        steps = list(PIPELINE_STEPS) if step is None else [step]
        label = "Full pipeline" if step is None else f"Step '{step}'"
        return self._enqueue(RUN_PIPELINE_TASK, None, {"steps": steps}, label)

    def run_for_job(self, job_id: int) -> TriggerResult:
        return self._enqueue(RUN_JOB_TASK, job_id, {}, f"Pipeline for job {job_id}")

    def _enqueue(
        self,
        task: str,
        job_id: int | None,
        options: Mapping[str, object],
        label: str,
    ) -> TriggerResult:
        payload = {**options, "trigger": "manual"}
        if self._queue.has_pending(task=task, job_id=job_id, payload=payload):
            return TriggerResult(False, None, f"{label} is already queued or running")
        run_id = self._new_run_id()
        item = self._queue.enqueue(
            idempotency_key=f"manual:{task}:{job_id or '-'}:{run_id}",
            run_id=run_id,
            task=task,
            job_id=job_id,
            payload=payload,
        )
        if item is None:
            return TriggerResult(False, None, f"{label} is already queued")
        return TriggerResult(True, run_id, f"{label} queued (run {str(run_id)[:8]})")
