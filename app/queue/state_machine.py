"""Pure job-status transitions used exclusively by the pipeline runner."""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum


class JobStatus(StrEnum):
    FOUND = "found"
    SCORED = "scored"
    TAILORED = "tailored"
    FILLED = "filled"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    SKIPPED = "skipped"
    FAILED = "failed"
    NEEDS_MANUAL = "needs_manual"


class InvalidStatusTransition(ValueError):
    pass


_ACTIVE_STATUSES = frozenset(
    {
        JobStatus.FOUND,
        JobStatus.SCORED,
        JobStatus.TAILORED,
        JobStatus.FILLED,
        JobStatus.APPROVED,
    }
)


def _transition(
    current: JobStatus | str,
    target: JobStatus,
    allowed_from: Collection[JobStatus],
) -> JobStatus:
    current_status = JobStatus(current)
    if current_status == target:
        return target
    if current_status not in allowed_from:
        raise InvalidStatusTransition(
            f"Cannot transition job from {current_status.value} to {target.value}"
        )
    return target


def mark_scored(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.SCORED, {JobStatus.FOUND})


def mark_tailored(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.TAILORED, {JobStatus.SCORED})


def mark_filled(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.FILLED, {JobStatus.TAILORED})


def mark_approved(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.APPROVED, {JobStatus.FILLED})


def mark_submitted(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.SUBMITTED, {JobStatus.APPROVED})


def mark_skipped(current: JobStatus | str) -> JobStatus:
    return _transition(
        current,
        JobStatus.SKIPPED,
        {
            JobStatus.FOUND,
            JobStatus.SCORED,
            JobStatus.TAILORED,
            JobStatus.FILLED,
        },
    )


def mark_failed(current: JobStatus | str) -> JobStatus:
    return _transition(current, JobStatus.FAILED, _ACTIVE_STATUSES)


def mark_needs_manual(current: JobStatus | str) -> JobStatus:
    return _transition(
        current,
        JobStatus.NEEDS_MANUAL,
        {
            JobStatus.FOUND,
            JobStatus.SCORED,
            JobStatus.TAILORED,
            JobStatus.FILLED,
        },
    )
