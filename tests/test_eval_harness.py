import json
from typing import Any

from app.llm.client import InvalidLLMOutput
from app.llm.schemas import ScorerOutput, TailorOutput
from app.steps.latex import LatexCompileError
from tests.evals.harness import (
    base_cv_tex,
    evaluate_scorer,
    evaluate_tailor,
    load_cases,
    regressions,
)


def test_eval_sets_have_ten_to_twenty_cases() -> None:
    for name in ("scorer_cases.json", "tailor_cases.json"):
        cases = load_cases(name)
        assert 10 <= len(cases) <= 20
        assert len({case["id"] for case in cases}) == len(cases)


class ScoringClient:
    """Answers each case with the midpoint of its expected range, except one."""

    def __init__(self, wrong_id: str | None = None, invalid_id: str | None = None) -> None:
        self.ranges = {
            case["job_description"]: case for case in load_cases("scorer_cases.json")
        }
        self.wrong_id = wrong_id
        self.invalid_id = invalid_id

    def generate(self, **kwargs: Any) -> ScorerOutput:
        case = self.ranges[kwargs["untrusted_data"]["job_description"]]
        if case["id"] == self.invalid_id:
            raise InvalidLLMOutput("fixture")
        score = 10 if case["id"] == self.wrong_id else (case["expected_min"] + case["expected_max"]) // 2
        return ScorerOutput(score=score, reasons=["fixture"], missing_skills=[])


def test_scorer_eval_counts_agreement_and_schema_validity() -> None:
    perfect = evaluate_scorer(ScoringClient(), model="fixture")
    worse = evaluate_scorer(
        ScoringClient(wrong_id="injection-attempt", invalid_id="ios-swift"), model="fixture"
    )

    assert perfect.summary()["agreement"] == perfect.cases == 12
    assert (worse.schema_valid, worse.agreement) == (11, 10)
    assert "injection-attempt: score 10 outside 1-3" in worse.failures
    assert regressions(perfect, worse) == [
        "schema_valid fell from 12 to 11",
        "agreement fell from 12 to 10",
    ]


class TailoringClient:
    """Returns the base CV, optionally inventing an employer or too many skills."""

    def __init__(self, invent: bool = False) -> None:
        self.invent = invent

    def generate(self, **kwargs: Any) -> TailorOutput:
        limits = json.loads(kwargs["untrusted_data"]["limits"])
        tex = base_cv_tex()
        skills: list[dict[str, Any]] = []
        if self.invent and "Google" in kwargs["untrusted_data"]["job_description"]:
            tex = tex.replace("Contoso Health", "Google")
            skills = [{"skill": "Go", "est_days": 30, "plan": "x"}] * (limits["max_added_skills"] + 1)
        return TailorOutput(tex=tex, changes=[], added_skills=skills)


class FixtureCompiler:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def compile(self, tex: str) -> bytes:
        if self.fail:
            raise LatexCompileError("pdflatex exited with status 1", "! error")
        return b"%PDF"


def test_tailor_eval_flags_invented_entities_limits_and_compile_errors() -> None:
    clean = evaluate_tailor(TailoringClient(), FixtureCompiler(), model="fixture")
    broken = evaluate_tailor(TailoringClient(invent=True), FixtureCompiler(fail=True), model="fixture")

    assert (clean.cases, clean.violations, clean.compiled) == (10, 0, 10)
    assert broken.compiled == 0
    assert any("name: Google" in failure for failure in broken.failures)
    assert any("skills > M=3" in failure for failure in broken.failures)
    assert any("days > N=7" in failure for failure in broken.failures)
    assert "violations rose from 0 to" in regressions(clean, broken)[0]
