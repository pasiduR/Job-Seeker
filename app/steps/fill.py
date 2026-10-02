"""Pipeline step: open the application form, run the filler, store the trace."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.browser.page import FormPage
from app.browser.stop_conditions import site_of
from app.queue.pipeline_runner import StepOutcome
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion
from app.steps.form_filler import FillOutcome, FillResult, FormFiller
from app.steps.form_mapper import FieldAnswer
from app.steps.latex import latex_to_text


SCREENSHOT_ROOT = Path("data/screenshots")
OpenForm = Callable[[str], AbstractContextManager[FormPage]]


def application_url(job_url: str) -> str:
    """The form URL for ATS postings whose application lives on a sub-page."""

    parts = urlsplit(job_url)
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    if host == "jobs.lever.co" and not path.endswith("/apply"):
        return f"{parts.scheme}://{host}{path}/apply"
    if host == "jobs.ashbyhq.com" and not path.endswith("/application"):
        return f"{parts.scheme}://{host}{path}/application"
    return job_url


def answers_json(answers: Iterable[FieldAnswer]) -> list[dict[str, Any]]:
    return [
        {
            "field_id": answer.field.field_id,
            "label": answer.field.label,
            "type": answer.field.type,
            "required": answer.field.required,
            "value": answer.value,
            "source": answer.source.value,
            "flag": answer.flag,
        }
        for answer in answers
    ]


class FillConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class NotificationQueue(Protocol):
    def queue(self, *, job_id: int, kind: str, payload: Mapping[str, Any]) -> None: ...


class PostgresNotificationQueue:
    """Adds a pending push notification; ``app.notify`` delivers it."""

    def __init__(self, connection: FillConnection) -> None:
        self._connection = connection

    def queue(self, *, job_id: int, kind: str, payload: Mapping[str, Any]) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO notifications (job_id, channel, payload)
                SELECT %s, 'push', %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM notifications
                    WHERE job_id = %s AND payload->>'kind' = %s
                )
                """,
                (job_id, json.dumps({"kind": kind, **payload}), job_id, kind),
            )


class FormFillStore(Protocol):
    def save_fill(
        self, *, job_id: int, cv_version_id: int, form_url: str, result: FillResult
    ) -> None: ...


class PostgresFormFillStore:
    def __init__(self, connection: FillConnection) -> None:
        self._connection = connection

    def save_fill(
        self, *, job_id: int, cv_version_id: int, form_url: str, result: FillResult
    ) -> None:
        """Replace the job's trace and fill result so re-runs never duplicate."""

        with self._connection.transaction():
            self._connection.execute("DELETE FROM form_traces WHERE job_id = %s", (job_id,))
            for entry in result.trace:
                self._connection.execute(
                    """
                    INSERT INTO form_traces (
                        job_id, sequence_number, tool, input, result, screenshot_path
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        entry.sequence,
                        entry.tool,
                        json.dumps(entry.input),
                        json.dumps(entry.result),
                        entry.screenshot_path,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO form_fills (
                    job_id, cv_version_id, outcome, reason, form_url, answers,
                    screenshot_path
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET
                    cv_version_id = EXCLUDED.cv_version_id,
                    outcome = EXCLUDED.outcome,
                    reason = EXCLUDED.reason,
                    form_url = EXCLUDED.form_url,
                    answers = EXCLUDED.answers,
                    screenshot_path = EXCLUDED.screenshot_path,
                    updated_at = now()
                """,
                (
                    job_id,
                    cv_version_id,
                    result.outcome.value,
                    result.reason,
                    form_url,
                    json.dumps(answers_json(result.answers)),
                    result.screenshot_path,
                ),
            )


class FillService:
    def __init__(
        self,
        *,
        filler: FormFiller,
        store: FormFillStore,
        open_form: OpenForm,
        notifications: NotificationQueue,
        screenshot_root: Path = SCREENSHOT_ROOT,
    ) -> None:
        self._filler = filler
        self._store = store
        self._open_form = open_form
        self._notifications = notifications
        self._screenshot_root = screenshot_root

    def run(
        self,
        *,
        job_id: int,
        job_url: str,
        profile: Mapping[str, Any],
        cv: CVVersion,
    ) -> StepOutcome:
        form_url = application_url(job_url)
        if site_of(form_url) == "linkedin.com":
            # Never open LinkedIn in automation (Easy Apply included): notify
            # so the person applies by hand.
            self._notifications.queue(
                job_id=job_id, kind="apply_manually", payload={"url": form_url}
            )
            return StepOutcome(JobStatus.NEEDS_MANUAL, "LinkedIn job: apply manually")
        if not re.match(r"https?://", form_url):
            return StepOutcome(JobStatus.NEEDS_MANUAL, "job has no http(s) application URL")

        with self._open_form(form_url) as page:
            result = self._filler.run(
                job_id=job_id,
                page=page,
                profile=profile,
                cv_text=latex_to_text(cv.tex),
                cv_pdf=Path(cv.pdf_path),
                screenshot_dir=self._screenshot_root / f"job-{job_id}",
            )
        self._store.save_fill(
            job_id=job_id, cv_version_id=cv.id, form_url=form_url, result=result
        )
        if result.outcome == FillOutcome.FILLED:
            return StepOutcome(JobStatus.FILLED)
        return StepOutcome(JobStatus.NEEDS_MANUAL, result.reason)
