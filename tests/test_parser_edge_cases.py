"""Edge cases for source parsers, dedupe, and the queue not covered elsewhere."""

from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.http import HttpResponse
from app.queue.pipeline_runner import PipelineRunner, PipelineStep, RunResult, StepOutcome
from app.queue.postgres import PostgresQueue, QueueItem
from app.queue.state_machine import JobStatus
from app.queue.worker import Worker
from app.sources.ats_api import AtsApiSource
from app.sources.jobspy import JobSpyError, JobSpySource
from app.sources.rss import parse_feed
from app.sources.scraper import PostgresJobStore, ScraperService, SearchFilter
from app.sources.types import JobListing
from tests.test_queue import FakePipelineStore, FakeQueue


RUN_ID = UUID("6e0c3d7a-1f2b-4c5d-8e9f-a0b1c2d3e4f5")


class RecordingConnection:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[tuple[Any, ...]]:
        self.statements.append((" ".join(query.split()), params))
        return self.rows


def test_atom_feed_entries_are_normalized(project_root: Path) -> None:
    content = (project_root / "tests/fixtures/jobs_feed_atom.xml").read_bytes()

    listings = parse_feed(content, default_company="Acme")

    assert [listing.title for listing in listings] == ["Site Reliability Engineer", "Data Engineer"]
    assert listings[0].description == "Run\nKubernetes\nclusters."
    assert listings[0].source_job_id == "urn:acme:job:42"
    assert listings[1].source_job_id == "https://acme.example/jobs/43"
    assert listings[1].posted_at is not None
    assert {listing.company for listing in listings} == {"Acme"}


def test_feed_entry_without_required_fields_is_rejected() -> None:
    feed = b"<rss><channel><item><title>Only a title</title></item></channel></rss>"

    with pytest.raises(ValueError, match="link"):
        parse_feed(feed, default_company="Acme")


class StaticHttp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def request(self, source: str, method: str, url: str, **_: Any) -> HttpResponse:
        return HttpResponse(status=200, headers={}, body=self.body)


@pytest.mark.parametrize(
    "body",
    [b'{"jobs": "not a list"}', b'{"jobs": [{"id": 1, "title": "No URL"}]}'],
)
def test_malformed_ats_payloads_raise(body: bytes) -> None:
    with pytest.raises(ValueError):
        AtsApiSource(StaticHttp(body)).greenhouse(board_token="acme", company="Acme")


def test_jobspy_retries_then_gives_up_after_three_attempts() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky(**_: object) -> object:
        attempts.append(1)
        raise ConnectionError("blocked")

    source = JobSpySource(scraper=flaky, timeout_seconds=5, sleeper=sleeps.append)

    with pytest.raises(JobSpyError):
        source.search(site_names=["indeed"], search_term="python", location=None, results_wanted=5)
    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]


def test_store_conflicts_count_as_duplicates() -> None:
    listing = JobListing(
        title="Python Engineer",
        company="Acme",
        url="https://jobs.example.com/1?utm_source=x",
        description="Python.",
        source_name="fixture",
    )
    connection = RecordingConnection(rows=[])

    result = ScraperService(PostgresJobStore(connection)).save_matches(
        source_id=3, listings=[listing], search_filter=SearchFilter()
    )

    query, params = connection.statements[0]
    assert "ON CONFLICT DO NOTHING" in query
    assert params is not None and params[4] == "https://jobs.example.com/1"
    assert (result.saved, result.duplicates) == (0, 1)


def _item(attempts: int, task: str = "run_job") -> QueueItem:
    return QueueItem(id=5, run_id=RUN_ID, job_id=1, task=task, payload={}, attempts=attempts)


def test_queue_retries_with_backoff_then_fails() -> None:
    connection = RecordingConnection()
    queue = PostgresQueue(connection)

    queue.retry_or_fail(_item(attempts=1), "w1", "boom", max_attempts=3)
    queue.retry_or_fail(_item(attempts=3), "w1", "boom", max_attempts=3)

    (retry_sql, retry_params), (fail_sql, fail_params) = connection.statements
    assert "status = 'queued'" in retry_sql and retry_params == (2, "boom", 5, "w1")
    assert "status = 'failed'" in fail_sql and fail_params == ("boom", 5, "w1")


def test_worker_retries_items_with_unknown_tasks() -> None:
    queue = FakeQueue(_item(attempts=1, task="unknown"))

    assert Worker(queue, "w1", {}).run_once() is True
    assert queue.succeeded == []
    assert queue.retried and queue.retried[0][0] == 5


def test_worker_reports_empty_queue() -> None:
    assert Worker(FakeQueue(None), "w1", {}).run_once() is False


def test_pipeline_stops_after_a_skip() -> None:
    store = FakePipelineStore()
    calls: list[str] = []
    steps = (
        PipelineStep("score", frozenset({JobStatus.FOUND}), lambda _: calls.append("score") or StepOutcome(JobStatus.SKIPPED)),
        PipelineStep("tailor", frozenset({JobStatus.SCORED, JobStatus.SKIPPED}), lambda _: calls.append("tailor") or StepOutcome(JobStatus.TAILORED)),
    )

    result = PipelineRunner(store, steps).run(run_id=RUN_ID, job_id=1, trigger="manual")

    assert result == RunResult.COMPLETED
    assert calls == ["score"]
    assert store.status == JobStatus.SKIPPED
