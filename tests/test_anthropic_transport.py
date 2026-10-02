import json
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel, Field

from app.config import RuntimeSettings, SecretSettings
from app.llm.client import (
    AnthropicTransport,
    LLMCallLogger,
    LLMClient,
    LLMNotConfigured,
    LLMProviderError,
    TokenPrices,
    TransientLLMError,
)


SONNET_PRICES = TokenPrices(
    input_usd_per_mtok=Decimal("3"), output_usd_per_mtok=Decimal("15")
)
API_URL = "https://claude.example.invalid/v1/messages"


class ScoreFixture(BaseModel):
    score: int = Field(ge=1, le=10)
    reasons: list[str]
    missing_skills: list[str]


class FakeMessages:
    def __init__(self, outcomes: list[Message | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RecordingLogConnection:
    def __init__(self) -> None:
        self.params: list[tuple[object, ...]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[Any]:
        assert params is not None
        self.params.append(params)
        return []


@pytest.fixture
def replies(project_root: Path) -> dict[str, Message]:
    raw = json.loads(
        (project_root / "tests/fixtures/anthropic_messages.json").read_text(
            encoding="utf-8"
        )
    )
    return {name: Message.model_validate(body) for name, body in raw.items()}


def status_error(cls: type[anthropic.APIStatusError], status: int) -> Exception:
    response = httpx2.Response(status, request=httpx2.Request("POST", API_URL))
    return cls(f"HTTP {status}", response=response, body=None)


def transport_for(
    outcomes: list[Message | Exception],
) -> tuple[AnthropicTransport, FakeMessages]:
    messages = FakeMessages(outcomes)
    return AnthropicTransport(messages, max_tokens=16000, prices=SONNET_PRICES), messages


def test_maps_text_usage_and_cost(replies: dict[str, Message]) -> None:
    transport, messages = transport_for([replies["json_reply"]])

    response = transport.complete(model="claude-sonnet-4-6", prompt="Score this job")

    assert json.loads(response.content)["score"] == 8
    assert response.input_tokens == 1200
    assert response.output_tokens == 300
    # 1200 * $3/M + 300 * $15/M
    assert response.cost_usd == Decimal("0.0081")
    assert messages.calls == [
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": "Score this job"}],
        }
    ]


def test_strips_a_markdown_code_fence(replies: dict[str, Message]) -> None:
    transport, _ = transport_for([replies["fenced_reply"]])

    content = transport.complete(model="m", prompt="p").content

    assert json.loads(content)["missing_skills"] == ["Kafka"]


@pytest.mark.parametrize("name", ["truncated_reply", "refusal_reply"])
def test_truncated_or_refused_output_is_not_retried(
    replies: dict[str, Message], name: str
) -> None:
    transport, _ = transport_for([replies[name]])

    with pytest.raises(LLMProviderError):
        transport.complete(model="m", prompt="p")


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.InternalServerError, 500),
        status_error(anthropic.APIStatusError, 529),
        anthropic.APITimeoutError(request=httpx2.Request("POST", API_URL)),
        anthropic.APIConnectionError(request=httpx2.Request("POST", API_URL)),
    ],
)
def test_retryable_failures_become_transient(error: Exception) -> None:
    transport, _ = transport_for([error])

    with pytest.raises(TransientLLMError):
        transport.complete(model="m", prompt="p")


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.BadRequestError, 400),
        status_error(anthropic.AuthenticationError, 401),
        status_error(anthropic.PermissionDeniedError, 403),
        status_error(anthropic.NotFoundError, 404),
    ],
)
def test_client_errors_are_not_retried(error: Exception) -> None:
    transport, _ = transport_for([error])

    with pytest.raises(LLMProviderError, match=r"HTTP 4\d\d"):
        transport.complete(model="m", prompt="p")


def test_llm_client_retries_transient_errors_and_logs_cost(
    replies: dict[str, Message],
) -> None:
    transport, messages = transport_for(
        [status_error(anthropic.RateLimitError, 429), replies["json_reply"]]
    )
    log = RecordingLogConnection()
    client = LLMClient(transport, LLMCallLogger(log), sleeper=lambda _: None)

    result = client.generate(
        schema=ScoreFixture,
        job_id=7,
        step="scorer",
        prompt_version="scorer_v1",
        model="claude-sonnet-4-6",
        prompt="Score",
        untrusted_data={"job_description": "Python role"},
    )

    assert result.score == 8
    assert len(messages.calls) == 2
    failed, succeeded = log.params
    assert failed[8] is False and "RateLimitError: HTTP 429" in str(failed[9])
    assert succeeded[4:7] == (1200, 300, Decimal("0.0081"))
    assert succeeded[8] is True


def test_from_config_requires_an_api_key() -> None:
    secrets = SecretSettings(_env_file=None, anthropic_api_key=None)

    with pytest.raises(LLMNotConfigured):
        AnthropicTransport.from_config(secrets, RuntimeSettings())


def test_from_config_targets_the_configured_endpoint_and_workspace() -> None:
    secrets = SecretSettings(
        _env_file=None,
        anthropic_api_key="test-key",
        anthropic_base_url="https://aws-external-anthropic.us-east-1.api.aws",
        anthropic_workspace_id="wrkspc_test",
    )
    settings = RuntimeSettings(
        llm_input_usd_per_mtok=1.5, llm_output_usd_per_mtok=7.5, llm_max_tokens=4000
    )

    transport = AnthropicTransport.from_config(secrets, settings)

    client = transport._messages._client  # type: ignore[attr-defined]
    assert str(client.base_url).startswith(
        "https://aws-external-anthropic.us-east-1.api.aws"
    )
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_test"
    assert client.max_retries == 0
    assert client.timeout == 120.0
    assert transport._max_tokens == 4000  # type: ignore[attr-defined]
    assert transport._prices == TokenPrices(Decimal("1.5"), Decimal("7.5"))  # type: ignore[attr-defined]
