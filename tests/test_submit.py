from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest

from app.browser.page import PlaywrightFormPage
from app.queue.state_machine import JobStatus
from app.steps.form_mapper import RESUME_UPLOAD
from app.steps.submit import PostgresSubmitStore, SubmissionPlan, SubmitService


FORM_URL = "https://boards.greenhouse.io/acme/jobs/1"


def answer(label: str, type_: str, value: Any, source: str = "profile", required: bool = False) -> dict[str, Any]:
    return {"field_id": "x", "label": label, "type": type_, "required": required,
            "value": value, "source": source, "flag": None}


APPROVED_ANSWERS = (
    answer("First name *", "text", "Jane", required=True),
    answer("Email *", "email", "jane.perera@example.com", required=True),
    answer("Country", "select", "Sri Lanka"),
    answer("Resume *", "file", RESUME_UPLOAD, "cv", required=True),
    answer("Why do you want to work here?", "textarea", "I enjoy reliable services.", "drafted"),
)


class MemorySubmitStore:
    def __init__(self, plan: SubmissionPlan | None, submitted_today: int = 0) -> None:
        self.plan = plan
        self.today = submitted_today
        self.applications: dict[int, dict[str, Any]] = {}

    def get_plan(self, job_id: int) -> SubmissionPlan | None:
        return self.plan

    def submitted_today(self, lane: str) -> int:
        return self.today + sum(1 for row in self.applications.values() if row["lane"] == lane)

    def reserve(self, *, job_id: int, cv_version_id: int, lane: str) -> bool:
        if job_id in self.applications:
            return False
        self.applications[job_id] = {"cv_version_id": cv_version_id, "lane": lane, "screenshot": None}
        return True

    def release(self, job_id: int) -> None:
        self.applications.pop(job_id, None)

    def record_screenshot(self, job_id: int, path: str | None) -> None:
        self.applications[job_id]["screenshot"] = path


def make_plan(tmp_path: Path, **changes: Any) -> SubmissionPlan:
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    values: dict[str, Any] = {
        "job_id": 3,
        "status": JobStatus.APPROVED,
        "form_url": FORM_URL,
        "cv_version_id": 11,
        "cv_pdf_path": str(pdf),
        "answers": APPROVED_ANSWERS,
        "next_buttons": ("Next",),
    }
    values.update(changes)
    return SubmissionPlan(**values)


class FixtureBrowser:
    """Serves the two-step fixture form for any URL and records what happened."""

    def __init__(self, project_root: Path, html: str | None = None) -> None:
        self.html = html or (project_root / "tests/fixtures/forms/two_step_form.html").read_text(encoding="utf-8")
        self.opened: list[str] = []
        self.submitted: bool | None = None
        self.values: dict[str, str] = {}

    @contextmanager
    def open_form(self, url: str) -> Iterator[PlaywrightFormPage]:
        from playwright.sync_api import sync_playwright

        self.opened.append(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(self.html)
                try:
                    yield PlaywrightFormPage(page, timeout_ms=5000)
                finally:
                    self.submitted = page.evaluate("window.submitted === true")
                    self.values = page.evaluate(
                        "Object.fromEntries([...document.querySelectorAll("
                        "'input:not([type=file]), select, textarea')].map(el => [el.id, el.value]))"
                    )
            finally:
                browser.close()


def service(store: MemorySubmitStore, browser: FixtureBrowser, tmp_path: Path) -> SubmitService:
    return SubmitService(store=store, open_form=browser.open_form, screenshot_root=tmp_path / "shots")


def test_approved_application_is_replayed_and_submitted_once(tmp_path: Path, project_root: Path) -> None:
    store = MemorySubmitStore(make_plan(tmp_path))
    browser = FixtureBrowser(project_root)

    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)

    assert outcome.status == JobStatus.SUBMITTED
    assert browser.submitted is True
    assert browser.values["first_name"] == "Jane"
    assert browser.values["country"] == "Sri Lanka"
    assert store.applications[3]["cv_version_id"] == 11
    assert Path(store.applications[3]["screenshot"]).is_file()

    again = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)
    assert (again.status, again.error) == (
        JobStatus.NEEDS_MANUAL, "an application is already recorded for this job"
    )
    assert len(browser.opened) == 1  # the second run never opened the form


@pytest.mark.parametrize("status", [JobStatus.FILLED, JobStatus.TAILORED, JobStatus.SUBMITTED])
def test_only_approved_jobs_can_be_submitted(tmp_path: Path, project_root: Path, status: JobStatus) -> None:
    store = MemorySubmitStore(make_plan(tmp_path, status=status))
    browser = FixtureBrowser(project_root)

    with pytest.raises(ValueError, match="not approved"):
        service(store, browser, tmp_path).run(job_id=3, daily_cap=10)
    assert browser.opened == [] and store.applications == {}


def test_daily_cap_keeps_the_job_approved_without_opening_the_form(tmp_path: Path, project_root: Path) -> None:
    store = MemorySubmitStore(make_plan(tmp_path), submitted_today=10)
    browser = FixtureBrowser(project_root)

    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)

    assert (outcome.status, outcome.error) == (JobStatus.APPROVED, "batch daily cap of 10 reached")
    assert browser.opened == [] and store.applications == {}


def test_changed_form_releases_the_reservation_without_submitting(tmp_path: Path, project_root: Path) -> None:
    answers = tuple(a for a in APPROVED_ANSWERS if a["label"] != "Email *")
    store = MemorySubmitStore(make_plan(tmp_path, answers=answers))
    browser = FixtureBrowser(project_root)

    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)

    assert outcome.status == JobStatus.NEEDS_MANUAL
    assert "no approved answer for 'Email *'" in (outcome.error or "")
    assert browser.submitted is False
    assert store.applications == {}


def test_missing_next_button_aborts_before_submit(tmp_path: Path, project_root: Path) -> None:
    store = MemorySubmitStore(make_plan(tmp_path, next_buttons=("Continue",)))
    browser = FixtureBrowser(project_root)

    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)

    assert "cannot find the 'Continue' button" in (outcome.error or "")
    assert browser.submitted is False and store.applications == {}


def test_captcha_on_replay_aborts_before_submit(tmp_path: Path, project_root: Path) -> None:
    html = FixtureBrowser(project_root).html.replace("<body>", "<body><div class='g-recaptcha'></div>")
    store = MemorySubmitStore(make_plan(tmp_path))
    browser = FixtureBrowser(project_root, html)

    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)

    assert outcome.error == "not submitted: ReplayAborted: CAPTCHA detected"
    assert store.applications == {}


def test_failure_after_the_click_keeps_the_reservation(tmp_path: Path, project_root: Path) -> None:
    store = MemorySubmitStore(make_plan(tmp_path))
    browser = FixtureBrowser(project_root)
    original = PlaywrightFormPage.screenshot

    def broken_screenshot(self: PlaywrightFormPage, path: Path) -> None:
        raise RuntimeError("browser crashed")

    PlaywrightFormPage.screenshot = broken_screenshot  # type: ignore[method-assign]
    try:
        outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=10)
    finally:
        PlaywrightFormPage.screenshot = original  # type: ignore[method-assign]

    assert outcome.status == JobStatus.NEEDS_MANUAL
    assert "submit clicked but the result is unclear" in (outcome.error or "")
    assert 3 in store.applications  # never retried automatically


def test_no_filled_form_means_needs_manual(tmp_path: Path, project_root: Path) -> None:
    outcome = service(MemorySubmitStore(None), FixtureBrowser(project_root), tmp_path).run(job_id=3, daily_cap=10)

    assert (outcome.status, outcome.error) == (JobStatus.NEEDS_MANUAL, "no filled form to submit")


class ScriptedConnection:
    def __init__(self, results: list[list[tuple[Any, ...]]]) -> None:
        self.results = iter(results)
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[tuple[Any, ...]]:
        self.queries.append((" ".join(query.split()), params))
        return next(self.results)


def test_postgres_store_reserves_with_the_unique_job_constraint() -> None:
    connection = ScriptedConnection([[(1,)], []])
    store = PostgresSubmitStore(connection)

    assert store.reserve(job_id=3, cv_version_id=11, lane="batch") is True
    assert store.reserve(job_id=3, cv_version_id=11, lane="batch") is False
    assert "ON CONFLICT (job_id) DO NOTHING" in connection.queries[0][0]
    assert connection.queries[0][1] == (3, 11, "batch")


def test_postgres_store_loads_plan_with_click_texts() -> None:
    connection = ScriptedConnection([
        [("approved", FORM_URL, 11, "/cvs/job-3.pdf", '[{"label": "Email *"}]')],
        [({"ok": True, "text": "Next"},), ('{"ok": true, "text": "Continue"}',)],
    ])

    plan = PostgresSubmitStore(connection).get_plan(3)

    assert plan is not None
    assert (plan.status, plan.cv_version_id, plan.next_buttons) == (JobStatus.APPROVED, 11, ("Next", "Continue"))
    assert plan.answers == ({"label": "Email *"},)
    assert "f.outcome = 'filled'" in connection.queries[0][0]
