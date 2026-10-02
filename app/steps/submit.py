"""Submit an approved application. Deterministic code only: no LLM here.

Order of guards:
1. The job must be ``approved``.
2. The lane's daily cap (from settings) must not be reached.
3. An ``applications`` row is reserved first; its unique ``job_id`` makes a
   second submit impossible even across workers or re-runs.
4. The approved answers are replayed onto a fresh page and only then is the
   final submit button clicked. A failure before that click releases the
   reservation; a failure after it keeps the row and asks a person to check.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.browser.fields import FormField
from app.browser.page import FormPage, PageButton
from app.browser.stop_conditions import find_blocker
from app.queue.pipeline_runner import StepOutcome
from app.queue.state_machine import JobStatus
from app.steps.form_mapper import RESUME_UPLOAD


Lane = Literal["batch", "fast_lane"]
OpenForm = Callable[[str], AbstractContextManager[FormPage]]
SCREENSHOT_ROOT = Path("data/screenshots")


class AlreadyApplied(RuntimeError):
    """The applications row already exists for this job."""


class ReplayAborted(RuntimeError):
    """The form could not be replayed safely; nothing was submitted."""


@dataclass(frozen=True)
class SubmissionPlan:
    job_id: int
    status: JobStatus
    form_url: str
    cv_version_id: int
    cv_pdf_path: str
    answers: tuple[dict[str, Any], ...]
    next_buttons: tuple[str, ...]


class SubmitStore(Protocol):
    def get_plan(self, job_id: int) -> SubmissionPlan | None: ...

    def submitted_today(self, lane: Lane) -> int: ...

    def reserve(self, *, job_id: int, cv_version_id: int, lane: Lane, daily_cap: int) -> bool: ...

    def release(self, job_id: int) -> None: ...

    def record_screenshot(self, job_id: int, path: str | None) -> None: ...


class SubmitConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresSubmitStore:
    def __init__(self, connection: SubmitConnection, *, timezone_name: str = "UTC") -> None:
        self._connection = connection
        self._timezone_name = timezone_name

    def _rows(self, query: str, params: tuple[object, ...]) -> list[tuple[Any, ...]]:
        with self._connection.transaction():
            return list(self._connection.execute(query, params))

    def get_plan(self, job_id: int) -> SubmissionPlan | None:
        rows = self._rows(
            """
            SELECT j.status, f.form_url, f.cv_version_id, c.pdf_path, f.answers
            FROM form_fills f
            JOIN jobs j ON j.id = f.job_id
            JOIN cv_versions c ON c.id = f.cv_version_id
            WHERE f.job_id = %s AND f.outcome = 'filled'
            """,
            (job_id,),
        )
        if not rows:
            return None
        status, form_url, cv_version_id, pdf_path, answers = rows[0]
        clicks = self._rows(
            """
            SELECT result FROM form_traces
            WHERE job_id = %s AND tool = 'click_next' AND result ? 'ok'
            ORDER BY sequence_number
            """,
            (job_id,),
        )
        decoded = answers if isinstance(answers, list) else json.loads(answers)
        return SubmissionPlan(
            job_id=job_id,
            status=JobStatus(status),
            form_url=str(form_url),
            cv_version_id=int(cv_version_id),
            cv_pdf_path=str(pdf_path),
            answers=tuple(decoded),
            next_buttons=tuple(str(_json(row[0])["text"]) for row in clicks),
        )

    def submitted_today(self, lane: Lane) -> int:
        rows = self._rows(
            """
            SELECT count(*) FROM applications
            WHERE lane = %s AND submitted_at >=
                (date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s)
            """,
            (lane, self._timezone_name, self._timezone_name),
        )
        return int(rows[0][0])

    def reserve(self, *, job_id: int, cv_version_id: int, lane: Lane, daily_cap: int) -> bool:
        with self._connection.transaction():
            # Count + reservation is serialized per lane, including across workers.
            self._connection.execute("SELECT pg_advisory_xact_lock(%s)",
                                     (1_906_151_934 if lane == "batch" else 1_906_151_935,))
            rows = list(self._connection.execute("""
                INSERT INTO applications (job_id, cv_version_id, lane)
                SELECT %s, %s, %s WHERE
                    (SELECT status FROM jobs WHERE id = %s) = 'approved'
                    AND (SELECT count(*) FROM applications WHERE lane = %s AND submitted_at >=
                        (date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s)) < %s
                ON CONFLICT (job_id) DO NOTHING RETURNING id
            """, (job_id, cv_version_id, lane, job_id, lane, self._timezone_name,
                   self._timezone_name, daily_cap)))
            return bool(rows)

    def release(self, job_id: int) -> None:
        self._rows("DELETE FROM applications WHERE job_id = %s RETURNING id", (job_id,))

    def record_screenshot(self, job_id: int, path: str | None) -> None:
        self._rows(
            "UPDATE applications SET screenshot_path = %s WHERE job_id = %s RETURNING id",
            (path, job_id),
        )


def _json(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else json.loads(value)


class SubmitService:
    def __init__(
        self,
        *,
        store: SubmitStore,
        open_form: OpenForm,
        screenshot_root: Path = SCREENSHOT_ROOT,
    ) -> None:
        self._store = store
        self._open_form = open_form
        self._screenshot_root = screenshot_root

    def run(self, *, job_id: int, daily_cap: int, lane: Lane = "batch") -> StepOutcome:
        plan = self._store.get_plan(job_id)
        if plan is None:
            return StepOutcome(JobStatus.NEEDS_MANUAL, "no filled form to submit")
        if plan.status != JobStatus.APPROVED:
            raise ValueError(f"job {job_id} is {plan.status.value}, not approved")
        if self._store.submitted_today(lane) >= daily_cap:
            # Stays approved; a later run submits it once the cap resets.
            return StepOutcome(JobStatus.APPROVED, f"{lane} daily cap of {daily_cap} reached")
        if not self._store.reserve(job_id=job_id, cv_version_id=plan.cv_version_id, lane=lane, daily_cap=daily_cap):
            if self._store.submitted_today(lane) >= daily_cap:
                return StepOutcome(JobStatus.APPROVED, f"{lane} daily cap of {daily_cap} reached")
            return StepOutcome(
                JobStatus.NEEDS_MANUAL, "an application is already recorded for this job"
            )

        screenshot = self._screenshot_root / f"job-{job_id}" / "submitted.png"
        clicked = False
        try:
            with self._open_form(plan.form_url) as page:
                start_url = page.url  # after any redirects
                submit_button = replay(page, plan, Path(plan.cv_pdf_path))
                clicked = True
                page.click(submit_button)
                page.screenshot(screenshot)
                blocker = find_blocker(
                    url=page.url, start_url=start_url, signals=page.signals()
                )
        except Exception as exc:
            if not clicked:
                self._store.release(job_id)
                return StepOutcome(
                    JobStatus.NEEDS_MANUAL, f"not submitted: {type(exc).__name__}: {exc}"[:500]
                )
            return StepOutcome(
                JobStatus.NEEDS_MANUAL,
                f"submit clicked but the result is unclear ({type(exc).__name__}); check manually",
            )
        self._store.record_screenshot(job_id, str(screenshot))
        if blocker is not None:
            return StepOutcome(
                JobStatus.NEEDS_MANUAL, f"submit clicked, then: {blocker}; check manually"
            )
        return StepOutcome(JobStatus.SUBMITTED)


def replay(page: FormPage, plan: SubmissionPlan, cv_pdf: Path) -> PageButton:
    """Re-enter the approved answers page by page; return the final submit button.

    Raises ReplayAborted, before anything is submitted, if the form no longer
    matches what was approved.
    """

    unused = list(plan.answers)
    start_url = page.url
    pages = list(plan.next_buttons) + [None]
    for next_text in pages:
        blocker = find_blocker(url=page.url, start_url=start_url, signals=page.signals())
        if blocker is not None:
            raise ReplayAborted(blocker)
        for form_field in page.fields():
            answer = _take_answer(unused, form_field)
            usable = answer is not None and answer["source"] != "unknown"
            if not usable:
                if form_field.required:
                    raise ReplayAborted(f"form changed: no approved answer for {form_field.label!r}")
                continue
            assert answer is not None
            if answer["value"] == RESUME_UPLOAD:
                page.upload(form_field, cv_pdf)
            else:
                page.fill(form_field, answer["value"])
        buttons = page.buttons()
        if next_text is not None:
            matches = [b for b in buttons if b.text == next_text and not b.looks_like_submit]
            if len(matches) != 1:
                raise ReplayAborted(f"form changed: cannot find the {next_text!r} button")
            page.click(matches[0])
            continue
        submits = [button for button in buttons if button.looks_like_submit]
        if len(submits) != 1:
            raise ReplayAborted(f"expected one submit button, found {len(submits)}")
        return submits[0]
    raise AssertionError("unreachable")


def _take_answer(unused: list[dict[str, Any]], form_field: FormField) -> dict[str, Any] | None:
    for index, answer in enumerate(unused):
        if answer["label"] == form_field.label and answer["type"] == form_field.type:
            return unused.pop(index)
    return None
