from contextlib import nullcontext
from decimal import Decimal
from typing import Any

import pytest

from app.config import RuntimeSettings, SecretSettings
from app.llm.client import AnthropicTransport, LLMResponse
from app.queue import __main__ as entrypoint
from app.queue.state_machine import JobStatus


SCORE_JSON = '{"score": 8, "reasons": ["Python match"], "missing_skills": []}'


class RecordingConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[Any]:
        self.queries.append((" ".join(query.split()), params))
        return []


class FixtureTransport:
    def __init__(self) -> None:
        self.models: list[str] = []

    def complete(self, *, model: str, prompt: str) -> LLMResponse:
        self.models.append(model)
        return LLMResponse(
            content=SCORE_JSON, input_tokens=100, output_tokens=20, cost_usd=Decimal("0.0006")
        )


def secrets_with_key() -> SecretSettings:
    return SecretSettings(
        _env_file=None,
        anthropic_api_key="test-key",
        anthropic_base_url="https://aws-external-anthropic.us-east-1.api.aws",
        anthropic_workspace_id="wrkspc_test",
    )


def test_without_an_api_key_llm_steps_stay_unwired() -> None:
    connection = RecordingConnection()
    secrets = SecretSettings(_env_file=None, anthropic_api_key=None)

    assert entrypoint.llm_steps(connection, secrets, RuntimeSettings()) is None  # type: ignore[arg-type]

    tasks = entrypoint.build_tasks(connection, RuntimeSettings(), secrets)  # type: ignore[arg-type]
    assert tasks._scorer is None and tasks._tailor is None
    assert tasks._dispatcher._career_pages is None


def test_with_an_api_key_the_worker_gets_scorer_tailor_and_career_pages() -> None:
    connection = RecordingConnection()
    settings = RuntimeSettings(llm_model="claude-sonnet-4-6", jobspy_results_wanted=15)

    tasks = entrypoint.build_tasks(connection, settings, secrets_with_key())  # type: ignore[arg-type]

    assert tasks._scorer is not None and tasks._scorer._model == "claude-sonnet-4-6"
    assert tasks._tailor is not None and tasks._tailor._model == "claude-sonnet-4-6"
    assert tasks._dispatcher._career_pages is not None
    assert tasks._dispatcher._career_pages._model == "claude-sonnet-4-6"
    assert tasks._dispatcher._jobspy_results_wanted == 15


def test_wired_scorer_logs_the_call_and_saves_the_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FixtureTransport()
    monkeypatch.setattr(
        AnthropicTransport, "from_config", classmethod(lambda cls, secrets, settings: transport)
    )
    connection = RecordingConnection()
    steps = entrypoint.llm_steps(
        connection,  # type: ignore[arg-type]
        secrets_with_key(),
        RuntimeSettings(llm_model="claude-sonnet-4-6"),
    )
    assert steps is not None

    decision = steps.scorer.run(
        job_id=42,
        job_description="Backend Python engineer",
        base_cv_text="Python, PostgreSQL",
        filters={"roles": ["Backend Engineer"]},
        threshold=7,
    )

    assert decision.status == JobStatus.SCORED
    assert transport.models == ["claude-sonnet-4-6"]
    llm_logs = [params for query, params in connection.queries if "INSERT INTO llm_calls" in query]
    assert llm_logs and llm_logs[0][:4] == (42, "scorer", "scorer_v1", "claude-sonnet-4-6")
    assert llm_logs[0][6] == Decimal("0.0006")
    assert any("UPDATE jobs" in query for query, _ in connection.queries)
