import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from app.llm.client import InvalidLLMOutput
from app.llm.schemas import FormAnswer, FormMapperOutput
from tests.evals.harness import (
    FieldExtractor,
    evaluate_form_mapper,
    load_cases,
    offline_field_extractor,
    regressions,
)


@pytest.fixture(scope="module")
def extract() -> Iterator[FieldExtractor]:
    with offline_field_extractor() as extractor:
        yield extractor


def test_form_eval_set_has_ten_to_twenty_real_forms() -> None:
    spec: Any = load_cases("form_mapper_cases.json")
    assert 10 <= len(spec["cases"]) <= 20
    assert len({case["form"] for case in spec["cases"]}) == len(spec["cases"])


def test_every_form_renders_offline_with_its_core_fields(extract: FieldExtractor) -> None:
    spec: Any = load_cases("form_mapper_cases.json")
    from tests.evals.harness import EVALS_DIR

    for case in spec["cases"]:
        fields = extract((EVALS_DIR / "forms" / case["form"]).read_text(encoding="utf-8"))
        labels = " | ".join(field.label.lower() for field in fields)
        assert len(fields) >= 8, case["id"]
        assert "email" in labels and "resume" in labels, case["id"]


class RuleFollowingClient:
    """Answers like a careful model: expected values, otherwise unknown."""

    def __init__(self, *, guess_gender: bool = False, invalid_form: str | None = None) -> None:
        spec: Any = load_cases("form_mapper_cases.json")
        self.rules = spec["rules"] + [rule for case in spec["cases"] for rule in case["rules"]]
        self.guess_gender = guess_gender
        self.invalid_form = invalid_form

    def generate(self, **kwargs: Any) -> FormMapperOutput:
        answers = []
        for line in kwargs["untrusted_data"]["form_fields"].splitlines():
            field = json.loads(line)
            if self.invalid_form and self.invalid_form in field["label"].lower():
                raise InvalidLLMOutput("fixture")
            rule = next((r for r in self.rules if re.search(r["label"], field["label"], re.IGNORECASE)), None)
            if self.guess_gender and re.search("gender", field["label"], re.IGNORECASE):
                value = field["options"][0] if field["options"] else "Female"
                answers.append(FormAnswer(field_id=field["field_id"], value=value, source="profile"))
            elif rule and rule["expect"] not in {"unknown", "@tailored_cv_pdf"}:
                answers.append(FormAnswer(field_id=field["field_id"], value=rule["expect"], source="profile"))
            else:
                answers.append(FormAnswer(field_id=field["field_id"], value=None, source="unknown"))
        return FormMapperOutput(answers=answers)


def test_careful_answers_agree_with_every_expectation(extract: FieldExtractor) -> None:
    report = evaluate_form_mapper(RuleFollowingClient(), extract, model="fixture")

    assert report.schema_valid == report.cases == 13
    assert report.violations == 0, report.failures
    assert report.checks > 100
    assert report.agreement == report.checks, report.failures


def test_guessed_demographics_count_as_violations_and_regressions(extract: FieldExtractor) -> None:
    careful = evaluate_form_mapper(RuleFollowingClient(), extract, model="fixture")
    guessing = evaluate_form_mapper(
        RuleFollowingClient(guess_gender=True, invalid_form="singapore citizen"), extract, model="fixture"
    )

    assert guessing.schema_valid == 12
    assert guessing.violations > 0
    assert any("discarded" in failure and "Gender" in failure for failure in guessing.failures)
    problems = regressions(careful, guessing)
    assert problems[0].startswith("violations rose from 0 to")
    assert "schema_valid fell from 13 to 12" in problems
