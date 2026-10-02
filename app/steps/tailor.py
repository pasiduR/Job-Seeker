"""CV tailoring step: reorder and reword the base CV for one job."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.llm.schemas import TailorOutput
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion


PROMPT_VERSION = "tailor_v1"
PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"

SkillPlacement = Literal["skills_section", "currently_learning"]


class TailorClient(Protocol):
    def generate(
        self,
        *,
        schema: type[TailorOutput],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> TailorOutput: ...


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


class Tailor:
    def __init__(self, *, llm: TailorClient, model: str) -> None:
        self._llm = llm
        self._model = model

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
            return TailorDecision(status=JobStatus.TAILORED, used_base=True)

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
        return TailorDecision(
            status=JobStatus.TAILORED, used_base=False, output=output
        )
