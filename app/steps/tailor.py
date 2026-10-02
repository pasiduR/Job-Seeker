"""CV tailoring step: reorder and reword the base CV for one job."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.llm.schemas import AddedSkill, LatexFixOutput, TailorOutput
from app.queue.state_machine import JobStatus
from app.steps.base_cv import DEFAULT_CV_DIR, CVVersion, PdfCompiler, write_pdf
from app.steps.cv_checks import (
    SkillPlacement,
    apply_skill_placement,
    find_invented_entities,
    find_unsafe_commands,
    limit_added_skills,
    placement_terms,
)
from app.steps.latex import LatexCompileError


PROMPT_VERSION = "tailor_v1"
PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"
FIX_PROMPT_VERSION = "latex_fix_v1"
FIX_PROMPT_PATH = (
    Path(__file__).parents[1] / "llm" / "prompts" / f"{FIX_PROMPT_VERSION}.md"
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class TailorClient(Protocol):
    def generate(
        self,
        *,
        schema: type[SchemaT],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> SchemaT: ...


class TailoredCVStore(Protocol):
    def save_tailored(
        self,
        *,
        job_id: int,
        tex: str,
        pdf_path: str,
        diff_from_base: str,
        skills: Sequence[AddedSkill],
    ) -> int: ...


class TailorConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresTailoredCVStore:
    def __init__(self, connection: TailorConnection) -> None:
        self._connection = connection

    def save_tailored(
        self,
        *,
        job_id: int,
        tex: str,
        pdf_path: str,
        diff_from_base: str,
        skills: Sequence[AddedSkill],
    ) -> int:
        """Upsert the job's CV and replace its skills so re-runs never duplicate."""

        with self._connection.transaction():
            rows = self._connection.execute(
                """
                INSERT INTO cv_versions (job_id, is_base, tex, pdf_path, diff_from_base)
                VALUES (%s, FALSE, %s, %s, %s)
                ON CONFLICT (job_id) WHERE job_id IS NOT NULL
                DO UPDATE SET tex = EXCLUDED.tex,
                              pdf_path = EXCLUDED.pdf_path,
                              diff_from_base = EXCLUDED.diff_from_base
                RETURNING id
                """,
                (job_id, tex, pdf_path, diff_from_base),
            )
            cv_version_id = int(next(iter(rows))[0])
            self._connection.execute(
                "DELETE FROM skills_to_learn WHERE job_id = %s", (job_id,)
            )
            for skill in skills:
                self._connection.execute(
                    """
                    INSERT INTO skills_to_learn (
                        job_id, skill, estimated_days, learning_plan
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (job_id, skill.skill, skill.est_days, skill.plan),
                )
        return cv_version_id


@dataclass(frozen=True)
class TailorSettings:
    skip_threshold: int
    max_skill_days: int
    max_added_skills: int
    placement: SkillPlacement


@dataclass(frozen=True)
class TailorDecision:
    status: JobStatus
    used_base: bool
    output: TailorOutput | None = None
    tex: str | None = None
    pdf: bytes | None = None
    pdf_path: str | None = None
    cv_version_id: int | None = None
    added_skills: tuple[AddedSkill, ...] = ()
    dropped_skills: tuple[str, ...] = ()
    error: str | None = None


class Tailor:
    def __init__(
        self,
        *,
        llm: TailorClient,
        compiler: PdfCompiler,
        store: TailoredCVStore,
        model: str,
        storage_dir: Path = DEFAULT_CV_DIR,
    ) -> None:
        self._llm = llm
        self._compiler = compiler
        self._store = store
        self._model = model
        self._storage_dir = storage_dir

    def run(
        self,
        *,
        job_id: int,
        job_description: str,
        job_score: int,
        base_cv: CVVersion,
        settings: TailorSettings,
    ) -> TailorDecision:
        if job_score >= settings.skip_threshold:
            return TailorDecision(
                status=JobStatus.TAILORED,
                used_base=True,
                pdf_path=base_cv.pdf_path,
                cv_version_id=base_cv.id,
            )

        output = self._llm.generate(
            schema=TailorOutput,
            job_id=job_id,
            step="tailor",
            prompt_version=PROMPT_VERSION,
            model=self._model,
            prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            untrusted_data={
                "base_cv_tex": base_cv.tex,
                "job_description": job_description,
                "limits": json.dumps(
                    {
                        "max_skill_days": settings.max_skill_days,
                        "max_added_skills": settings.max_added_skills,
                    }
                ),
            },
        )
        decision = _check_output(output, base_cv.tex, settings)
        if decision.status == JobStatus.FAILED or decision.tex is None:
            return decision
        decision = self._compile_with_one_fix(job_id, base_cv.tex, decision, settings)
        if decision.status == JobStatus.FAILED:
            return decision
        return self._save(job_id, base_cv.tex, decision)

    def _save(
        self, job_id: int, base_tex: str, decision: TailorDecision
    ) -> TailorDecision:
        assert decision.tex is not None and decision.pdf is not None
        pdf_path = self._storage_dir / f"job-{job_id}.pdf"
        write_pdf(pdf_path, decision.pdf)
        cv_version_id = self._store.save_tailored(
            job_id=job_id,
            tex=decision.tex,
            pdf_path=str(pdf_path),
            diff_from_base=diff_against_base(base_tex, decision.tex),
            skills=decision.added_skills,
        )
        return replace(decision, pdf_path=str(pdf_path), cv_version_id=cv_version_id)

    def _compile_with_one_fix(
        self,
        job_id: int,
        base_tex: str,
        decision: TailorDecision,
        settings: TailorSettings,
    ) -> TailorDecision:
        assert decision.tex is not None
        try:
            return replace(decision, pdf=self._compiler.compile(decision.tex))
        except LatexCompileError as first_error:
            fix = self._llm.generate(
                schema=LatexFixOutput,
                job_id=job_id,
                step="tailor_fix",
                prompt_version=FIX_PROMPT_VERSION,
                model=self._model,
                prompt=FIX_PROMPT_PATH.read_text(encoding="utf-8"),
                untrusted_data={
                    "tex": decision.tex,
                    "compile_log": first_error.log or str(first_error),
                },
            )

        problems = find_unsafe_commands(base_tex, fix.tex) + find_invented_entities(
            base_tex,
            fix.tex,
            allowed_terms=placement_terms(decision.added_skills, settings.placement),
        )
        if problems:
            return _failed(
                decision, "LaTeX fix rejected: " + "; ".join(problems)
            )
        try:
            pdf = self._compiler.compile(fix.tex)
        except LatexCompileError as second_error:
            return _failed(
                decision, f"LaTeX compile failed after one fix: {second_error}"
            )
        return replace(decision, tex=fix.tex, pdf=pdf)


def diff_against_base(base_tex: str, tailored_tex: str) -> str:
    return "".join(
        difflib.unified_diff(
            base_tex.splitlines(keepends=True),
            tailored_tex.splitlines(keepends=True),
            fromfile="base.tex",
            tofile="tailored.tex",
        )
    )


def _check_output(
    output: TailorOutput, base_tex: str, settings: TailorSettings
) -> TailorDecision:
    """Enforce the tailoring rules in code, whatever the prompt produced."""

    unsafe = find_unsafe_commands(base_tex, output.tex)
    if unsafe:
        return _rejected(output, "unsafe LaTeX commands: " + ", ".join(unsafe))
    invented = find_invented_entities(base_tex, output.tex)
    if invented:
        return _rejected(output, "invented entities: " + "; ".join(invented))

    limited = limit_added_skills(
        output.added_skills,
        base_tex=base_tex,
        max_days=settings.max_skill_days,
        max_count=settings.max_added_skills,
    )
    tex = apply_skill_placement(output.tex, limited.kept, settings.placement)
    leaked = find_invented_entities(
        base_tex, tex, allowed_terms=placement_terms(limited.kept, settings.placement)
    )
    if leaked:
        return _rejected(output, "invented entities: " + "; ".join(leaked))
    return TailorDecision(
        status=JobStatus.TAILORED,
        used_base=False,
        output=output,
        tex=tex,
        added_skills=tuple(limited.kept),
        dropped_skills=tuple(limited.dropped),
    )


def _rejected(output: TailorOutput, reason: str) -> TailorDecision:
    return TailorDecision(
        status=JobStatus.FAILED,
        used_base=False,
        output=output,
        error=f"Tailored CV rejected: {reason}",
    )


def _failed(decision: TailorDecision, error: str) -> TailorDecision:
    return replace(decision, status=JobStatus.FAILED, tex=None, pdf=None, error=error)
