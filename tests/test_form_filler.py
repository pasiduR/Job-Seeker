import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.browser.page import PlaywrightFormPage
from app.config import RuntimeSettings
from app.llm.schemas import FormAgentAction, FormAnswer, FormMapperOutput
from app.steps.form_filler import FillLimits, FillOutcome, FormFiller
from app.steps.form_mapper import FormMapper


PROFILE = {"first_name": "Jane", "email": "jane.perera@example.com", "country": "Sri Lanka"}
# What a well-behaved mapper returns, keyed by field label.
MAPPER_ANSWERS = {
    "First name *": ("Jane", "profile"),
    "Email *": ("jane.perera@example.com", "profile"),
    "Country": ("Sri Lanka", "profile"),
    "Why do you want to work here?": ("I enjoy building reliable Python services.", "drafted"),
}


class ScriptedLLM:
    """Answers mapper calls from MAPPER_ANSWERS and agent calls from a script."""

    def __init__(self, actions: list[dict[str, Any]], mapper_answers: dict[str, tuple[Any, str]] = MAPPER_ANSWERS) -> None:
        self.actions = iter(actions)
        self.mapper_answers = mapper_answers
        self.agent_inputs: list[dict[str, str]] = []

    def generate(self, *, schema: type, untrusted_data: dict[str, str], **_: Any) -> Any:
        if schema is FormMapperOutput:
            answers = []
            for line in untrusted_data["form_fields"].splitlines():
                asked = json.loads(line)
                value, source = self.mapper_answers.get(asked["label"], (None, "unknown"))
                answers.append(FormAnswer(field_id=asked["field_id"], value=value, source=source))
            return FormMapperOutput(answers=answers)
        self.agent_inputs.append(untrusted_data)
        return FormAgentAction(**next(self.actions))


@pytest.fixture
def browser_page(project_root: Path) -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                (project_root / "tests/fixtures/forms/two_step_form.html").read_text(encoding="utf-8")
            )
            yield page
        finally:
            browser.close()


def field_ids(llm_input: dict[str, str]) -> dict[str, str]:
    return {json.loads(line)["label"]: json.loads(line)["field_id"] for line in llm_input["fields"].splitlines()}


def button_ids(llm_input: dict[str, str]) -> dict[str, str]:
    return {json.loads(line)["text"]: json.loads(line)["button_id"] for line in llm_input["buttons"].splitlines()}


class AdaptiveLLM(ScriptedLLM):
    """Picks the next action from what the agent currently sees, like a real model would."""

    def __init__(
        self,
        plan: list[tuple[str, str | None]],
        mapper_answers: dict[str, tuple[Any, str]] = MAPPER_ANSWERS,
    ) -> None:
        super().__init__([], mapper_answers)
        self.plan = iter(plan)

    def generate(self, *, schema: type, untrusted_data: dict[str, str], **kwargs: Any) -> Any:
        if schema is FormMapperOutput:
            return super().generate(schema=schema, untrusted_data=untrusted_data, **kwargs)
        self.agent_inputs.append(untrusted_data)
        tool, target = next(self.plan)
        if tool in {"fill_field", "upload_file"}:
            return FormAgentAction(tool=tool, field_id=field_ids(untrusted_data).get(target or "", target))
        if tool == "click_next":
            return FormAgentAction(tool=tool, button_id=button_ids(untrusted_data).get(target or "", target))
        return FormAgentAction(tool=tool, reason=target or "")


def make_filler(llm: Any, limits: FillLimits = FillLimits()) -> FormFiller:
    return FormFiller(llm=llm, mapper=FormMapper(llm=llm, model="m"), model="m", limits=limits)


def run(filler: FormFiller, browser_page: Any, tmp_path: Path, profile: dict[str, Any] = PROFILE) -> Any:
    cv_pdf = tmp_path / "cv.pdf"
    cv_pdf.write_bytes(b"%PDF-1.4 fixture")
    return filler.run(
        job_id=5,
        page=PlaywrightFormPage(browser_page, timeout_ms=5000),
        profile=profile,
        cv_text="Python developer",
        cv_pdf=cv_pdf,
        screenshot_dir=tmp_path / "shots",
    )


def test_fills_a_two_step_form_and_stops_before_submit(browser_page: Any, tmp_path: Path) -> None:
    llm = AdaptiveLLM([
        ("fill_field", "First name *"),
        ("fill_field", "Email *"),
        ("fill_field", "Country"),
        ("click_next", "Next"),
        ("extract_fields", None),
        ("upload_file", "Resume *"),
        ("fill_field", "Why do you want to work here?"),
        ("click_next", "Submit application"),  # refused by code
        ("done", None),
    ])

    result = run(make_filler(llm), browser_page, tmp_path)

    assert (result.outcome, result.reason) == (FillOutcome.FILLED, "form filled; ready for review")
    assert browser_page.evaluate("window.submitted") is False
    assert browser_page.input_value("#first_name") == "Jane"
    assert browser_page.input_value("#country") == "Sri Lanka"
    assert browser_page.evaluate("document.getElementById('resume').files[0].name") == "cv.pdf"
    assert "reliable Python" in browser_page.input_value("#why")
    assert [entry.tool for entry in result.trace] == [
        "extract_fields", "fill_field", "fill_field", "fill_field", "click_next",
        "extract_fields", "upload_file", "fill_field", "click_next", "screenshot",
    ]
    refused = result.trace[8]
    assert "final submit" in refused.result["error"]
    assert result.screenshot_path and Path(result.screenshot_path).is_file()
    assert result.trace[-1].screenshot_path == result.screenshot_path
    assert '"final_submit": true' in llm.agent_inputs[-1]["buttons"]


def test_done_is_refused_until_required_fields_are_filled(browser_page: Any, tmp_path: Path) -> None:
    llm = AdaptiveLLM([("done", None), ("fill_field", "First name *"), ("fill_field", "Email *"), ("done", None)])

    result = run(make_filler(llm), browser_page, tmp_path)

    assert result.outcome == FillOutcome.FILLED
    assert "refused: required fields not filled: First name *, Email *" in llm.agent_inputs[1]["history"]


def test_unknown_required_answer_stops_as_needs_manual(browser_page: Any, tmp_path: Path) -> None:
    answers = {**MAPPER_ANSWERS, "Email *": (None, "unknown")}
    llm = ScriptedLLM([], mapper_answers=answers)

    result = run(make_filler(llm), browser_page, tmp_path)

    assert result.outcome == FillOutcome.NEEDS_MANUAL
    assert result.reason == "required field unanswered: Email *"
    assert llm.agent_inputs == []  # stopped before the agent acted


def test_values_only_come_from_approved_answers(browser_page: Any, tmp_path: Path) -> None:
    answers = {**MAPPER_ANSWERS, "Country": (None, "unknown")}
    llm = AdaptiveLLM(
        [("fill_field", "Country"), ("upload_file", "First name *"), ("stop", "stuck")], answers
    )

    result = run(make_filler(llm), browser_page, tmp_path)

    llm_errors = [entry.result.get("error") for entry in result.trace[1:]]
    assert llm_errors == [
        "ValueError: no approved answer for this field",
        "ValueError: only the tailored CV can be uploaded, to a file field",
    ]
    assert browser_page.input_value("#country") == ""
    assert (result.outcome, result.reason) == (FillOutcome.NEEDS_MANUAL, "agent stopped: stuck")


def test_step_limit_ends_in_needs_manual(browser_page: Any, tmp_path: Path) -> None:
    llm = AdaptiveLLM([("screenshot", None)] * 3)

    result = run(make_filler(llm, FillLimits(max_steps=3, max_pages=6)), browser_page, tmp_path)

    assert (result.outcome, result.reason) == (FillOutcome.NEEDS_MANUAL, "step limit of 3 reached")
    assert result.screenshot_path is None


def test_page_limit_ends_in_needs_manual(browser_page: Any, tmp_path: Path) -> None:
    llm = AdaptiveLLM([("click_next", "Next")])

    result = run(make_filler(llm, FillLimits(max_steps=25, max_pages=1)), browser_page, tmp_path)

    assert (result.outcome, result.reason) == (FillOutcome.NEEDS_MANUAL, "page limit of 1 reached")
    assert result.trace[-1].result["stopped"] is True


def test_limits_come_from_settings() -> None:
    assert FillLimits.from_settings(RuntimeSettings()) == FillLimits(max_steps=25, max_pages=6)
    custom = RuntimeSettings(form_max_steps=10, form_max_pages=2)
    assert FillLimits.from_settings(custom) == FillLimits(max_steps=10, max_pages=2)
