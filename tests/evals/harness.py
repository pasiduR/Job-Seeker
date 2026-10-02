"""Eval harness for the scorer and tailor prompts.

Run against a real LLM (once a transport is configured) to compare prompt
versions: schema validity, score agreement, and rule violations. The pytest
suite exercises this harness with fixture clients only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.llm.client import InvalidLLMOutput
from app.llm.schemas import ScorerOutput, TailorOutput
from app.steps.cv_checks import find_invented_entities, find_unsafe_commands
from app.steps.latex import LatexCompileError, latex_to_text
from app.steps.scorer import PROMPT_VERSION as SCORER_PROMPT_VERSION
from app.steps.scorer import Scorer
from app.steps.tailor import PROMPT_PATH as TAILOR_PROMPT_PATH
from app.steps.tailor import PROMPT_VERSION as TAILOR_PROMPT_VERSION


EVALS_DIR = Path(__file__).parent
FIXTURES_DIR = EVALS_DIR.parent / "fixtures"


class EvalClient(Protocol):
    def generate(self, **kwargs: Any) -> Any: ...


class Compiler(Protocol):
    def compile(self, tex: str) -> bytes: ...


@dataclass
class EvalReport:
    prompt_version: str
    cases: int = 0
    schema_valid: int = 0
    agreement: int = 0
    violations: int = 0
    compiled: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "cases": self.cases,
            "schema_valid": self.schema_valid,
            "agreement": self.agreement,
            "violations": self.violations,
            "compiled": self.compiled,
        }


def load_cases(name: str) -> list[dict[str, Any]]:
    return json.loads((EVALS_DIR / name).read_text(encoding="utf-8"))


def base_cv_tex() -> str:
    return (FIXTURES_DIR / "base_cv.tex").read_text(encoding="utf-8")


class _DiscardScores:
    def save_score(self, job_id: int, score: ScorerOutput) -> None:
        pass


def evaluate_scorer(client: EvalClient, *, model: str) -> EvalReport:
    report = EvalReport(prompt_version=SCORER_PROMPT_VERSION)
    scorer = Scorer(llm=client, store=_DiscardScores(), model=model)
    base_text = latex_to_text(base_cv_tex())
    for case in load_cases("scorer_cases.json"):
        report.cases += 1
        try:
            decision = scorer.run(
                job_id=0,
                job_description=case["job_description"],
                base_cv_text=base_text,
                filters={},
                threshold=7,
            )
        except InvalidLLMOutput:
            report.failures.append(f"{case['id']}: invalid schema")
            continue
        report.schema_valid += 1
        score = decision.output.score
        if case["expected_min"] <= score <= case["expected_max"]:
            report.agreement += 1
        else:
            report.failures.append(
                f"{case['id']}: score {score} outside "
                f"{case['expected_min']}-{case['expected_max']}"
            )
    return report


def tailor_violations(
    base_tex: str, output: TailorOutput, case: Mapping[str, Any]
) -> list[str]:
    """Rule breaks in the raw prompt output, before code enforcement repairs them."""

    problems = [f"unsafe: {name}" for name in find_unsafe_commands(base_tex, output.tex)]
    problems += find_invented_entities(base_tex, output.tex)
    if len(output.added_skills) > case["max_added_skills"]:
        problems.append(f"{len(output.added_skills)} skills > M={case['max_added_skills']}")
    problems += [
        f"{skill.skill}: {skill.est_days} days > N={case['max_skill_days']}"
        for skill in output.added_skills
        if skill.est_days > case["max_skill_days"]
    ]
    return problems


def evaluate_tailor(client: EvalClient, compiler: Compiler, *, model: str) -> EvalReport:
    report = EvalReport(prompt_version=TAILOR_PROMPT_VERSION)
    base_tex = base_cv_tex()
    prompt = TAILOR_PROMPT_PATH.read_text(encoding="utf-8")
    for case in load_cases("tailor_cases.json"):
        report.cases += 1
        limits = {
            "max_skill_days": case["max_skill_days"],
            "max_added_skills": case["max_added_skills"],
        }
        try:
            output = client.generate(
                schema=TailorOutput,
                job_id=None,
                step="tailor_eval",
                prompt_version=TAILOR_PROMPT_VERSION,
                model=model,
                prompt=prompt,
                untrusted_data={
                    "base_cv_tex": base_tex,
                    "job_description": case["job_description"],
                    "limits": json.dumps(limits),
                },
            )
        except InvalidLLMOutput:
            report.failures.append(f"{case['id']}: invalid schema")
            continue
        report.schema_valid += 1
        problems = tailor_violations(base_tex, output, case)
        report.violations += len(problems)
        report.failures += [f"{case['id']}: {problem}" for problem in problems]
        try:
            compiler.compile(output.tex)
        except LatexCompileError as exc:
            report.failures.append(f"{case['id']}: does not compile ({exc})")
        else:
            report.compiled += 1
    return report


def regressions(old: EvalReport, new: EvalReport) -> list[str]:
    """Reasons not to ship ``new``; an empty list means it is no worse."""

    problems: list[str] = []
    if new.violations > old.violations:
        problems.append(f"violations rose from {old.violations} to {new.violations}")
    for metric in ("schema_valid", "agreement", "compiled"):
        before, after = getattr(old, metric), getattr(new, metric)
        if after < before:
            problems.append(f"{metric} fell from {before} to {after}")
    return problems


__all__ = [
    "EvalReport",
    "evaluate_scorer",
    "evaluate_tailor",
    "regressions",
    "tailor_violations",
]
