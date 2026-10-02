import json
from pathlib import Path
from typing import Any

from app.llm.schemas import ScorerOutput
from app.queue.state_machine import JobStatus
from app.steps.scorer import PROMPT_VERSION, Scorer


class FixtureScorerClient:
    def __init__(self, output: ScorerOutput) -> None:
        self.output = output
        self.call: dict[str, Any] | None = None

    def generate(self, **kwargs: Any) -> ScorerOutput:
        self.call = kwargs
        return self.output


class FixtureScoreStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, ScorerOutput]] = []

    def save_score(self, job_id: int, score: ScorerOutput) -> None:
        self.saved.append((job_id, score))


def test_scorer_uses_threshold_and_persists_fixture_results(
    project_root: Path,
) -> None:
    cases = json.loads(
        (project_root / "tests/fixtures/scorer_cases.json").read_text(
            encoding="utf-8"
        )
    )
    for case in cases:
        output = ScorerOutput.model_validate(
            {
                "score": case["score"],
                "reasons": case["reasons"],
                "missing_skills": case["missing_skills"],
            }
        )
        client = FixtureScorerClient(output)
        store = FixtureScoreStore()
        scorer = Scorer(llm=client, store=store, model="fixture-model")

        decision = scorer.run(
            job_id=17,
            job_description="Python role requiring Kubernetes.",
            base_cv_text="Python API engineer.",
            filters={"roles": ["Python Engineer"], "remote": True},
            threshold=case["threshold"],
        )

        assert decision.status == JobStatus(case["expected_status"])
        assert store.saved == [(17, output)]
        assert client.call is not None
        assert client.call["prompt_version"] == PROMPT_VERSION
        assert set(client.call["untrusted_data"]) == {
            "job_description",
            "base_cv",
            "search_filters",
        }
