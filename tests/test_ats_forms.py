import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from app.browser.page import PlaywrightFormPage
from app.llm.schemas import FormAgentAction, FormAnswer, FormMapperOutput
from app.queue.tasks import PostgresWorkerStore
from app.steps.fill import application_form_url
from app.steps.form_filler import FillOutcome, FormFiller
from app.steps.form_mapper import FormMapper


@pytest.mark.parametrize(
    ("job_url", "source_job_id", "ats", "form_url"),
    [
        (
            "https://stripe.com/jobs/listing/backend/8172508?gh_jid=8172508",
            "8172508",
            ("greenhouse", "stripe"),
            "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8172508",
        ),
        (
            "https://jobs.lever.co/zoox/f4746da4",
            "f4746da4",
            ("lever", "zoox"),
            "https://jobs.lever.co/zoox/f4746da4/apply",
        ),
        ("https://jobs.ashbyhq.com/acme/9f1", "9f1", ("ashby", "acme"), "https://jobs.ashbyhq.com/acme/9f1/application"),
        ("https://www.indeed.com/viewjob?jk=1", "1", None, "https://www.indeed.com/viewjob?jk=1"),
        ("https://boards.greenhouse.io/acme/jobs/1", None, ("greenhouse", "acme"), "https://boards.greenhouse.io/acme/jobs/1"),
    ],
)
def test_application_form_url(job_url: str, source_job_id: str | None, ats: tuple[str, str] | None, form_url: str) -> None:
    assert application_form_url(job_url, source_job_id=source_job_id, ats=ats) == form_url


class OneRowConnection:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[tuple[Any, ...]]:
        assert "LEFT JOIN sources" in query
        return [self.row]


@pytest.mark.parametrize(
    ("source", "form_url"),
    [
        (("ats_board", "https://boards.greenhouse.io/stripe", {}, "Stripe"),
         "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8172508"),
        (("ats_board", "https://stripe.com/jobs", {"provider": "greenhouse", "board": "stripe"}, "Stripe"),
         "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=8172508"),
        (("ats_board", "https://careers.example.com", {}, "Unknown ATS"), "https://stripe.com/jobs/listing/8172508"),
        (("job_board", "https://remotive.com", {"adapter": "remotive"}, "Remotive"), "https://stripe.com/jobs/listing/8172508"),
        ((None, None, None, None), "https://stripe.com/jobs/listing/8172508"),
    ],
)
def test_worker_store_resolves_the_form_url_from_the_source(source: tuple[Any, ...], form_url: str) -> None:
    row = (7, "Backend role", 8, "https://stripe.com/jobs/listing/8172508", "8172508", *source, "batch", 1)

    job = PostgresWorkerStore(OneRowConnection(row)).get_job(7)  # type: ignore[arg-type]

    assert job.form_url == form_url


PROFILE = {
    "first_name": "Jane",
    "last_name": "Perera",
    "email": "jane.perera@example.com",
    "phone": "+94 77 123 4567",
    "location": {"city": "Colombo", "country": "Sri Lanka"},
    "how_did_you_hear": "Company careers page",
    "work_authorization": {"requires_visa_sponsorship": True},
    "consents": {"acknowledge_privacy_notice": True},
}
ANSWERS = {
    "First Name": ("Jane", "profile"),
    "Last Name": ("Perera", "profile"),
    "Email": ("jane.perera@example.com", "profile"),
    "Country": ("Sri Lanka", "profile"),
    "Phone": ("+94 77 123 4567", "profile"),
    "Location (City)": ("Colombo", "profile"),
    "How did you hear about this job?": ("Company careers page", "profile"),
    "Acknowledge/Confirm": (True, "profile"),
}


class FormLLM:
    """Mapper answers by label; the agent fills every ready field, then says done."""

    def generate(self, *, schema: type, untrusted_data: dict[str, str], **_: Any) -> Any:
        lines = [json.loads(line) for line in untrusted_data.get("form_fields", untrusted_data.get("fields", "")).splitlines()]
        if schema is FormMapperOutput:
            answers = []
            for field in lines:
                value, source = ANSWERS.get(field["label"], (None, "unknown"))
                if field["label"].startswith("Do you now or will you in the future require immigration"):
                    value, source = "Yes", "profile"
                answers.append(FormAnswer(field_id=field["field_id"], value=value, source=source))
            return FormMapperOutput(answers=answers)
        for field in lines:
            if not field["filled"] and field["answer"] in {"ready", "upload_cv"}:
                tool = "upload_file" if field["answer"] == "upload_cv" else "fill_field"
                return FormAgentAction(tool=tool, field_id=field["field_id"])
        return FormAgentAction(tool="done")


def test_real_greenhouse_form_is_filled_by_our_own_filler(tmp_path: Path, project_root: Path) -> None:
    from playwright.sync_api import sync_playwright

    html = (project_root / "tests/evals/forms/greenhouse_cloudflare.html").read_text(encoding="utf-8")
    cv_pdf = tmp_path / "cv.pdf"
    cv_pdf.write_bytes(b"%PDF-1.4 fixture")
    llm = FormLLM()
    filler = FormFiller(llm=llm, mapper=FormMapper(llm=llm, model="m"), model="m")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            result = filler.run(
                job_id=1,
                page=PlaywrightFormPage(page, timeout_ms=5000),
                profile=PROFILE,
                cv_text="Python developer",
                cv_pdf=cv_pdf,
                screenshot_dir=tmp_path / "shots",
            )
            values = {key: page.input_value(f"#{key}") for key in ("first_name", "last_name", "email", "phone")}
            resume = page.evaluate("document.getElementById('resume').files[0]?.name")
        finally:
            browser.close()

    assert result.outcome == FillOutcome.FILLED, result.reason
    assert values == {
        "first_name": "Jane", "last_name": "Perera",
        "email": "jane.perera@example.com", "phone": "+94 77 123 4567",
    }
    assert resume == "cv.pdf"
    flagged = {answer.field.label for answer in result.answers if answer.flag}
    assert "Acknowledge/Confirm" in flagged  # legal answers always need a human look
