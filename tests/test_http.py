import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.http import HttpClient, HttpResponse, HttpStatusError


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ResponseTransport:
    def __init__(self, statuses: Iterable[int], retry_after: int) -> None:
        self._statuses = iter(statuses)
        self.retry_after = retry_after
        self.timeouts: list[float] = []

    def __call__(self, request: object, timeout_seconds: float) -> HttpResponse:
        self.timeouts.append(timeout_seconds)
        status = next(self._statuses)
        headers = {"Retry-After": str(self.retry_after)} if status == 429 else {}
        return HttpResponse(status=status, headers=headers, body=b'{"ok": true}')


@pytest.fixture
def http_scenarios(project_root: Path) -> dict[str, object]:
    return json.loads(
        (project_root / "tests/fixtures/http_scenarios.json").read_text(
            encoding="utf-8"
        )
    )


def test_http_client_retries_transient_responses_with_timeout(
    http_scenarios: dict[str, object],
) -> None:
    clock = FakeClock()
    statuses = http_scenarios["transient_then_success"]
    assert isinstance(statuses, list)
    transport = ResponseTransport(
        statuses, int(http_scenarios["retry_after_seconds"])
    )
    client = HttpClient(
        timeout_seconds=12,
        source_minimum_intervals={},
        transport=transport,
        sleeper=clock.sleep,
        clock=clock,
    )

    response = client.request("greenhouse", "get", "https://example.invalid/jobs")

    assert response.status == 200
    assert response.json() == {"ok": True}
    assert transport.timeouts == [12, 12, 12]
    assert clock.sleeps == [1.0, 3.0]


def test_http_client_does_not_retry_permanent_failure(
    http_scenarios: dict[str, object],
) -> None:
    statuses = http_scenarios["permanent_failure"]
    assert isinstance(statuses, list)
    transport = ResponseTransport(statuses, 0)
    client = HttpClient(
        timeout_seconds=5,
        source_minimum_intervals={},
        transport=transport,
        sleeper=lambda _: None,
    )

    with pytest.raises(HttpStatusError):
        client.request("lever", "GET", "https://example.invalid/jobs")

    assert transport.timeouts == [5]


def test_http_client_applies_configured_rate_limit_per_source() -> None:
    clock = FakeClock()
    transport = ResponseTransport([200, 200, 200], 0)
    client = HttpClient(
        timeout_seconds=5,
        source_minimum_intervals={"rss": 2.5},
        transport=transport,
        sleeper=clock.sleep,
        clock=clock,
    )

    client.request("rss", "GET", "https://example.invalid/one")
    client.request("rss", "GET", "https://example.invalid/two")
    client.request("other", "GET", "https://example.invalid/three")

    assert clock.sleeps == [2.5]
