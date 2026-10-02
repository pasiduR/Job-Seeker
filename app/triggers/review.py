"""Review decisions (dashboard or push button) become queue items for the worker.

Only ``pipeline_runner`` changes job status, so Approve / Reject never touch
``jobs`` directly: they enqueue a ``review_decision`` item and the worker
applies it right away.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from uuid import UUID, uuid4

from app.triggers.manual import TriggerQueue, TriggerResult


REVIEW_DECISION_TASK = "review_decision"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ReviewDecisions:
    def __init__(
        self, queue: TriggerQueue, *, new_run_id: Callable[[], UUID] = uuid4
    ) -> None:
        self._queue = queue
        self._new_run_id = new_run_id

    def decide(self, job_id: int, decision: Decision) -> TriggerResult:
        label = f"{decision.value.capitalize()} for job {job_id}"
        payload = {"decision": decision.value, "trigger": "manual"}
        if self._queue.has_pending(task=REVIEW_DECISION_TASK, job_id=job_id, payload=payload):
            return TriggerResult(False, None, f"{label} is already queued")
        run_id = self._new_run_id()
        item = self._queue.enqueue(
            idempotency_key=f"review:{job_id}:{decision.value}:{run_id}",
            run_id=run_id,
            task=REVIEW_DECISION_TASK,
            job_id=job_id,
            payload=payload,
        )
        if item is None:
            return TriggerResult(False, None, f"{label} is already queued")
        return TriggerResult(True, run_id, f"{label} queued")
