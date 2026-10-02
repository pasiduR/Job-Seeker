import json
from contextlib import nullcontext

import pytest
from pydantic import ValidationError

from app.config import RuntimeSettings
from app.sources.rate_limit import PostgresRateLimiter
from app.http import HttpClient, HttpResponse
from app.sources.jobspy import JobSpySource
from tests.test_dashboard_pages import AUTH, make_client, post, store
from tests.test_submit import MemorySubmitStore, make_plan, FixtureBrowser, service, ScriptedConnection
from app.steps.submit import PostgresSubmitStore
from app.queue.state_machine import JobStatus


class PacingConnection:
    def __init__(self):
        self.reservations = {}
        self.queries = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params):
        self.queries.append((query, params))
        key, interval, _, _ = params
        previous = self.reservations.get(key, 0)
        self.reservations[key] = previous + interval
        return [(previous,)]


def test_pacing_survives_new_limiter_and_uses_specific_settings(project_root):
    intervals = json.loads((project_root / "tests/fixtures/rate_intervals.json").read_text())
    connection, sleeps = PacingConnection(), []
    PostgresRateLimiter(connection, intervals, sleeper=sleeps.append).wait("jobspy:indeed")
    PostgresRateLimiter(connection, intervals, sleeper=sleeps.append).wait("jobspy:indeed")
    limiter = PostgresRateLimiter(connection, intervals, sleeper=sleeps.append)
    limiter.wait("greenhouse:acme")
    limiter.wait("rss:feed")
    assert sleeps == [10.0]
    assert connection.queries[2][1][1] == 5
    assert connection.queries[3][1][1] == 2
    assert "greatest(now(), request_pacing.next_allowed_at)" in connection.queries[0][0]


def test_http_and_jobspy_pace_every_retry():
    keys = []
    class Limiter:
        def wait(self, source):
            keys.append(source)
    statuses = iter([429, 200])
    http = HttpClient(timeout_seconds=1, source_minimum_intervals={}, rate_limiter=Limiter(),
                      sleeper=lambda seconds: None, transport=lambda request, timeout: HttpResponse(next(statuses), {}, b"[]"))
    http.request("rss:feed", "GET", "https://example.com/feed")
    attempts = []
    def scrape(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise OSError("temporary")
        return []
    JobSpySource(timeout_seconds=1, rate_limiter=Limiter(), scraper=scrape, sleeper=lambda seconds: None).search(
        site_names=["indeed"], search_term="Python", location=None, results_wanted=1)
    assert keys == ["rss:feed", "rss:feed", "jobspy:indeed", "jobspy:indeed"]


def test_settings_pacing_json_is_validated(store, tmp_path):
    client = make_client(store, tmp_path)
    response = post(client, "/settings", {"source_request_intervals_seconds": '{"*": 3, "jobspy": 20}'})
    assert "error=" not in response.headers["location"]
    assert store.settings["source_request_intervals_seconds"]["jobspy"] == 20
    response = post(client, "/settings", {"source_request_intervals_seconds": '{"*": -1}'})
    assert "error=" in response.headers["location"]
    assert 'name="source_request_intervals_seconds"' in client.get("/settings", auth=AUTH).text


def test_cap_race_stays_approved_and_never_opens_browser(tmp_path, project_root):
    class RacedStore(MemorySubmitStore):
        def reserve(self, **kwargs):
            self.today = 1
            return False
    store = RacedStore(make_plan(tmp_path))
    browser = FixtureBrowser(project_root)
    outcome = service(store, browser, tmp_path).run(job_id=3, daily_cap=1, lane="fast_lane")
    assert outcome.status == JobStatus.APPROVED
    assert "fast_lane daily cap" in outcome.error
    assert not browser.opened


def test_daily_counts_use_configured_timezone():
    connection = ScriptedConnection([[(2,)]])
    assert PostgresSubmitStore(connection, timezone_name="Asia/Colombo").submitted_today("fast_lane") == 2
    assert connection.queries[0][1] == ("fast_lane", "Asia/Colombo", "Asia/Colombo")


@pytest.mark.parametrize("values", [{}, {"*": 0}, {"*": float('inf')}])
def test_invalid_pacing_settings(values):
    with pytest.raises(ValidationError):
        RuntimeSettings(source_request_intervals_seconds=values)
