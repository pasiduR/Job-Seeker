import json
from decimal import Decimal
from pathlib import Path

from app.llm.client import LLMResponse
from tests.evals.__main__ import compare, run_eval, save_result
from tests.evals.harness import base_cv_tex, load_cases


class CaseTransport:
    """Answers scorer prompts with each case's range midpoint and tailor prompts with the base CV."""

    def __init__(self) -> None:
        self.scores = {
            case["job_description"]: (case["expected_min"] + case["expected_max"]) // 2
            for case in load_cases("scorer_cases.json")
        }

    def complete(self, *, model: str, prompt: str) -> LLMResponse:
        if '"tex"' in prompt or "base_cv_tex" in prompt:
            body = {"tex": base_cv_tex(), "changes": [], "added_skills": []}
        else:
            # The sanitizer may strip injected lines, so match on the opening words.
            score = next(s for text, s in self.scores.items() if text[:30] in prompt)
            body = {"score": score, "reasons": ["fixture"], "missing_skills": []}
        return LLMResponse(
            content=json.dumps(body), input_tokens=1000, output_tokens=100, cost_usd=Decimal("0.0045")
        )


class PassingCompiler:
    def compile(self, tex: str) -> bytes:
        return b"%PDF"


def test_scorer_run_reports_summary_and_usage() -> None:
    result = run_eval("scorer", CaseTransport(), PassingCompiler(), model="fixture-model")

    assert result["summary"]["agreement"] == result["summary"]["cases"] == 12
    assert result["usage"] == {
        "calls": 12,
        "invalid_calls": 0,
        "input_tokens": 12000,
        "output_tokens": 1200,
        "cost_usd": "0.0540",
    }


def test_tailor_run_counts_compiles_and_violations() -> None:
    result = run_eval("tailor", CaseTransport(), PassingCompiler(), model="fixture-model")

    assert result["summary"]["compiled"] == result["summary"]["cases"] == 10
    assert result["summary"]["violations"] == 0


def test_saved_result_is_the_baseline_for_the_next_prompt_version(tmp_path: Path) -> None:
    result = run_eval("scorer", CaseTransport(), PassingCompiler(), model="fixture-model")
    path = save_result(result, tmp_path)

    assert path.name == "scorer_v1__fixture-model.json"
    assert compare(path, result) == []
    worse = {**result, "summary": {**result["summary"], "agreement": 9}}
    assert compare(path, worse) == ["agreement fell from 12 to 9"]
