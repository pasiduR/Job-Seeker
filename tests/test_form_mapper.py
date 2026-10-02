import json
from pathlib import Path
from typing import Any

import pytest

from app.browser.fields import FormField
from app.llm.schemas import AnswerSource, FormAnswer, FormMapperOutput
from app.steps.form_mapper import (
    RESUME_UPLOAD,
    FormMapper,
    SensitiveCategory,
    enforce_rules,
    sensitive_category,
)


def field(field_id: str, label: str, type_: str = "text", *, required: bool = False,
          options: list[str] | None = None) -> FormField:
    return FormField(
        field_id=field_id, label=label, type=type_, required=required,
        options=options or [], selector=f'[data-jobseeker-field="{field_id}"]',
    )


def answer(field_id: str, value: Any, source: str) -> FormAnswer:
    return FormAnswer(field_id=field_id, value=value, source=AnswerSource(source))


@pytest.fixture
def profile(project_root: Path) -> dict[str, Any]:
    return json.loads(
        (project_root / "tests/fixtures/form_mapper_profile.json").read_text(encoding="utf-8")
    )


def mapped(fields: list[FormField], answers: list[FormAnswer], profile: dict[str, Any]) -> dict[str, Any]:
    result = enforce_rules(fields, FormMapperOutput(answers=answers), profile)
    return {item.field.field_id: item for item in result.answers}


def test_supported_answers_are_kept_and_files_are_handled_in_code(profile: dict[str, Any]) -> None:
    fields = [
        field("f1", "First Name *", required=True),
        field("f2", "Email", "email", required=True),
        field("f3", "Country", "select", options=["Sri Lanka", "United Kingdom"]),
        field("f4", "Why do you want to work here?", "textarea"),
        field("f5", "Resume/CV", "file", required=True),
        field("f6", "Cover letter", "file"),
        field("f7", "Most recent employer"),
    ]
    result = enforce_rules(
        fields,
        FormMapperOutput(answers=[
            answer("f1", "Jane", "profile"),
            answer("f2", "jane.perera@example.com", "profile"),
            answer("f3", "sri lanka", "profile"),
            answer("f4", "I like building small, reliable services.", "drafted"),
            answer("f7", "Contoso Health", "cv"),
        ]),
        profile,
    )
    answers = {item.field.field_id: item for item in result.answers}

    assert answers["f1"].value == "Jane"
    assert answers["f3"].value == "Sri Lanka"  # canonical option text
    assert answers["f4"].source == AnswerSource.DRAFTED
    assert (answers["f5"].value, answers["f5"].source) == (RESUME_UPLOAD, AnswerSource.CV)
    assert answers["f6"].is_unknown and answers["f7"].source == AnswerSource.CV
    assert result.needs_manual_reasons == []
    assert {item.field.field_id for item in result.flagged} == {"f4", "f6"}


def test_unknown_or_missing_required_answers_need_manual(profile: dict[str, Any]) -> None:
    fields = [
        field("f1", "Notice period", required=True),
        field("f2", "Portfolio", required=True),
        field("f3", "Hobbies"),
    ]
    result = enforce_rules(
        fields,
        FormMapperOutput(answers=[
            answer("f1", None, "unknown"),
            answer("ghost", "x", "profile"),
            answer("f3", None, "unknown"),
        ]),
        profile,
    )

    assert result.needs_manual_reasons == [
        "required field unanswered: Notice period",
        "required field unanswered: Portfolio",
    ]


def test_profile_answers_must_exist_in_the_profile(profile: dict[str, Any]) -> None:
    answers = mapped(
        [field("f1", "Email", "email"), field("f2", "Phone", "tel")],
        [answer("f1", "jane@made-up.example", "profile"), answer("f2", "+94 77 123 4567", "profile")],
        profile,
    )

    assert answers["f1"].is_unknown and answers["f1"].flag == "profile answer not found in profile"
    assert answers["f2"].value == "+94 77 123 4567"


def test_choice_values_must_be_listed_options_and_never_drafted(profile: dict[str, Any]) -> None:
    answers = mapped(
        [
            field("f1", "Country", "select", options=["Sri Lanka", "India"]),
            field("f2", "Preferred team", "radio_group", options=["Platform", "Data"]),
            field("f3", "Stacks", "checkbox_group", options=["Python", "Go"]),
        ],
        [
            answer("f1", "Lanka", "profile"),
            answer("f2", "Platform", "drafted"),
            answer("f3", ["Python", "Rust"], "cv"),
        ],
        profile,
    )

    assert all(item.is_unknown for item in answers.values())
    assert answers["f2"].flag == "drafted answer for a choice field"


@pytest.mark.parametrize(
    ("label", "category"),
    [
        ("Are you legally authorized to work in the UK?", SensitiveCategory.WORK_AUTHORIZATION),
        ("Will you now or in the future require visa sponsorship?", SensitiveCategory.WORK_AUTHORIZATION),
        ("Expected salary (GBP)", SensitiveCategory.SALARY),
        ("Gender", SensitiveCategory.DEMOGRAPHIC),
        ("Do you identify as a veteran?", SensitiveCategory.DEMOGRAPHIC),
        ("Have you been convicted of a criminal offence?", SensitiveCategory.LEGAL),
        ("I agree to the privacy notice", SensitiveCategory.LEGAL),
        ("Homepage", None),
        ("Languages spoken", None),
        ("Why do you want to work here?", None),
    ],
)
def test_sensitive_questions_are_detected_by_code(label: str, category: SensitiveCategory | None) -> None:
    assert sensitive_category(label) == category


def test_sensitive_answers_need_a_matching_profile_field_and_are_flagged(profile: dict[str, Any]) -> None:
    work_auth = field("f1", "Will you require visa sponsorship?", "radio_group",
                      required=True, options=["Yes", "No"])
    salary = field("f2", "Expected salary")
    salary_guess = field("f3", "Desired pay")
    gender = field("f4", "Gender", "select", required=True, options=["Female", "Male"])
    consent = field("f5", "I agree to the privacy notice", "checkbox", required=True)
    from_cv = field("f6", "Are you eligible to work in Sri Lanka?", "radio_group", options=["Yes", "No"])

    result = enforce_rules(
        [work_auth, salary, salary_guess, gender, consent, from_cv],
        FormMapperOutput(answers=[
            answer("f1", "Yes", "profile"),
            answer("f2", "60000 GBP", "profile"),
            answer("f3", "70000", "profile"),
            answer("f4", "Female", "profile"),
            answer("f5", True, "profile"),
            answer("f6", "Yes", "cv"),
        ]),
        profile,
    )
    answers = {item.field.field_id: item for item in result.answers}

    assert answers["f1"].value == "Yes" and "confirm" in (answers["f1"].flag or "")
    assert answers["f2"].value == "60000 GBP" and answers["f2"].flag
    assert answers["f3"].is_unknown  # not the profile's salary
    assert answers["f4"].is_unknown  # profile has no demographic field
    assert answers["f5"].is_unknown  # no consent field in the profile
    assert answers["f6"].flag == "work_authorization question not answered from profile"
    assert result.needs_manual_reasons == [
        "required field unanswered: Gender",
        "required field unanswered: I agree to the privacy notice",
    ]


class RecordingClient:
    def __init__(self, output: FormMapperOutput) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> FormMapperOutput:
        self.calls.append(kwargs)
        return self.output


def test_mapper_sends_fields_one_per_line_without_selectors_or_files(profile: dict[str, Any]) -> None:
    client = RecordingClient(FormMapperOutput(answers=[answer("f1", "Jane", "profile")]))
    mapper = FormMapper(llm=client, model="claude-sonnet-4-6")

    result = mapper.run(
        job_id=9,
        fields=[field("f1", "First name", required=True), field("f2", "Resume", "file", required=True)],
        profile=profile,
        cv_text="Python developer",
    )

    call = client.calls[0]
    assert (call["step"], call["prompt_version"], call["job_id"]) == ("form_mapper", "form_map_v1", 9)
    lines = call["untrusted_data"]["form_fields"].splitlines()
    assert [json.loads(line) for line in lines] == [
        {"field_id": "f1", "label": "First name", "type": "text", "required": True, "options": []}
    ]
    assert "selector" not in call["untrusted_data"]["form_fields"]
    assert result.needs_manual_reasons == []


def test_mapper_skips_the_llm_when_only_files_are_asked(profile: dict[str, Any]) -> None:
    client = RecordingClient(FormMapperOutput(answers=[]))

    result = FormMapper(llm=client, model="m").run(
        job_id=1, fields=[field("f1", "CV", "file", required=True)], profile=profile, cv_text=""
    )

    assert client.calls == [] and result.answers[0].value == RESUME_UPLOAD
