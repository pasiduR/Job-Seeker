"""Schema-validated job scoring without direct status mutation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.llm.schemas import ScorerOutput
from app.queue.state_machine import JobStatus


PROMPT_VERSION = "scorer_v1"
PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"


class ScorerClient(Protocol):
    def generate(
        self,
        *,
        schema: type[ScorerOutput],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> ScorerOutput: ...


class ScoreStore(Protocol):
    def save_score(self, job_id: int, score: ScorerOutput) -> None: ...


class ScoreConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresScoreStore:
    def __init__(self, connection: ScoreConnection) -> None:
        self._connection = connection

    def save_score(self, job_id: int, score: ScorerOutput) -> None:
        details = {
            "reasons": score.reasons,
            "missing_skills": score.missing_skills,
        }
        with self._connection.transaction():
            self._connection.execute(
                """
                UPDATE jobs
                SET score = %s, score_details = %s, updated_at = now()
                WHERE id = %s
                """,
                (score.score, json.dumps(details), job_id),
            )


@dataclass(frozen=True)
class ScoringDecision:
    output: ScorerOutput
    status: JobStatus


class Scorer:
    def __init__(
        self,
        *,
        llm: ScorerClient,
        store: ScoreStore,
        model: str,
    ) -> None:
        self._llm = llm
        self._store = store
        self._model = model

    def run(
        self,
        *,
        job_id: int,
        job_description: str,
        base_cv_text: str,
        filters: Mapping[str, object],
        threshold: int,
    ) -> ScoringDecision:
        if not 1 <= threshold <= 10:
            raise ValueError("score threshold must be between 1 and 10")

        output = self._llm.generate(
            schema=ScorerOutput,
            job_id=job_id,
            step="scorer",
            prompt_version=PROMPT_VERSION,
            model=self._model,
            prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            untrusted_data={
                "job_description": job_description,
                "base_cv": base_cv_text,
                "search_filters": json.dumps(filters, sort_keys=True),
            },
        )
        self._store.save_score(job_id, output)
        status = (
            JobStatus.SCORED if output.score >= threshold else JobStatus.SKIPPED
        )
        return ScoringDecision(output=output, status=status)
