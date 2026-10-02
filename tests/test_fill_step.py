import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest

from app.browser.page import PlaywrightFormPage
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion
from app.steps.fill import (
    FillService,
    PostgresFormFillStore,
    PostgresNotificationQueue,
    application_url,
)
from app.steps.form_filler import FillLimits, FormFiller
from app.steps.form_mapper import FormMapper
from tests.test_form_filler import AdaptiveLLM


class RecordingConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[Any]:
        self.queries.append((" ".join(query.split()), params))
        return []


@pytest.mark.parametrize(
    ("job_url", "form_url"),
    [
        ("https://jobs.lever.co/acme/abc-123", "https://jobs.lever.co/acme/abc-123/apply"),
        ("https://jobs.lever.co/acme/abc-123/apply", "https://jobs.lever.co/acme/abc-123/apply"),
        ("https://jobs.ashbyhq.com/acme/9f1", "https://jobs.ashbyhq.com/acme/9f1/application"),
        ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/1"),
    ],
)
def test_application_url(job_url: str, form_url: str) -> None:
    assert application_url(job_url) == form_url


@pytest.fixture
def cv(tmp_path: Path, project_root: Path) -> CVVersion:
    pdf = tmp_path / "job-7.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    tex = (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")
    return CVVersion(id=42, tex=tex, pdf_path=str(pdf))


def fixture_form(project_root: Path, opened: list[str]) -> Any:
    @contextmanager
    def open_form(url: str) -> Iterator[PlaywrightFormPage]:
        from playwright.sync_api import sync_playwright

        opened.append(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(
                    (project_root / "tests/fixtures/forms/two_step_form.html").read_text(encoding="utf-8")
                )
                yield PlaywrightFormPage(page, timeout_ms=5000)
            finally:
                browser.close()

    return open_form


def service(llm: AdaptiveLLM, connection: RecordingConnection, open_form: Any, tmp_path: Path) -> FillService:
    return FillService(
        filler=FormFiller(llm=llm, mapper=FormMapper(llm=llm, model="m"), model="m", limits=FillLimits()),
        store=PostgresFormFillStore(connection),
        open_form=open_form,
        notifications=PostgresNotificationQueue(connection),
        screenshot_root=tmp_path / "shots",
    )


def test_filled_form_stores_trace_answers_and_screenshot(
    tmp_path: Path, project_root: Path, cv: CVVersion
) -> None:
    llm = AdaptiveLLM([
        ("fill_field", "First name *"),
        ("fill_field", "Email *"),
        ("click_next", "Next"),
        ("extract_fields", None),
        ("upload_file", "Resume *"),
        ("done", None),
    ])
    connection = RecordingConnection()
    opened: list[str] = []

    outcome = service(llm, connection, fixture_form(project_root, opened), tmp_path).run(
        job_id=7,
        job_url="https://jobs.lever.co/acme/abc",
        profile={"first_name": "Jane", "email": "jane.perera@example.com"},
        cv=cv,
    )

    assert outcome.status == JobStatus.FILLED
    assert opened == ["https://jobs.lever.co/acme/abc/apply"]
    queries = [query for query, _ in connection.queries]
    assert queries[0] == "DELETE FROM form_traces WHERE job_id = %s"
    traces = [params for query, params in connection.queries if query.startswith("INSERT INTO form_traces")]
    assert [row[2] for row in traces] == [
        "extract_fields", "fill_field", "fill_field", "click_next",
        "extract_fields", "upload_file", "screenshot",
    ]
    assert [row[1] for row in traces] == list(range(1, 8))
    final_shot = traces[-1][5]
    assert final_shot and Path(final_shot).is_file() and "job-7" in final_shot
    fill_row = next(params for query, params in connection.queries if query.startswith("INSERT INTO form_fills"))
    assert fill_row is not None
    job_id, cv_version_id, status, reason, form_url, answers, screenshot = fill_row
    assert (job_id, cv_version_id, status, screenshot) == (7, 42, "filled", final_shot)
    stored = {answer["label"]: answer for answer in json.loads(str(answers))}
    assert stored["First name *"]["value"] == "Jane"
    assert stored["Resume *"]["value"] == "@tailored_cv_pdf"


def test_needs_manual_result_is_stored_with_its_reason(
    tmp_path: Path, project_root: Path, cv: CVVersion
) -> None:
    llm = AdaptiveLLM([("stop", "login required")])
    connection = RecordingConnection()

    outcome = service(llm, connection, fixture_form(project_root, []), tmp_path).run(
        job_id=8,
        job_url="https://boards.greenhouse.io/acme/jobs/1",
        profile={"first_name": "Jane", "email": "jane.perera@example.com"},
        cv=cv,
    )

    assert (outcome.status, outcome.error) == (JobStatus.NEEDS_MANUAL, "agent stopped: login required")
    fill_row = next(params for query, params in connection.queries if query.startswith("INSERT INTO form_fills"))
    assert fill_row is not None and fill_row[2:4] == ("needs_manual", "agent stopped: login required")


def test_linkedin_jobs_are_never_opened(tmp_path: Path, cv: CVVersion) -> None:
    def refuse(url: str) -> Any:
        raise AssertionError("LinkedIn must not be opened")

    connection = RecordingConnection()
    outcome = service(AdaptiveLLM([]), connection, refuse, tmp_path).run(
        job_id=9, job_url="https://www.linkedin.com/jobs/view/123", profile={}, cv=cv
    )

    assert (outcome.status, outcome.error) == (JobStatus.NEEDS_MANUAL, "LinkedIn job: apply manually")
    [(query, params)] = connection.queries  # only the notification, no fill rows
    assert query.startswith("INSERT INTO notifications") and "WHERE NOT EXISTS" in query
    assert params is not None and params[0] == 9 and params[3] == "apply_manually"
    assert json.loads(str(params[1])) == {
        "kind": "apply_manually", "url": "https://www.linkedin.com/jobs/view/123"
    }
