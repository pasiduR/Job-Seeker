import copy
import json
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

import pytest

from app.queue.postgres import QueueItem
from app.config import RuntimeSettings
from app.sources.models import SourceRepository, SourceType
from app.sources.types import JobListing
from app.triggers.watcher import Watcher, enqueue_due
from tests.test_source_dispatch import source, dispatcher


class WatchConnection:
    def __init__(self):
        self.last = None
        self.jobs = {}
        self.queue = []
        self.polled = False
        self.fail_enqueue = False

    @contextmanager
    def transaction(self):
        state = copy.deepcopy((self.last, self.jobs, self.queue, self.polled))
        try:
            yield
        except Exception:
            self.last, self.jobs, self.queue, self.polled = state
            raise

    def execute(self, query, params=None):
        sql = " ".join(query.split())
        if "SELECT sub.id, sub.polling_interval_minutes" in sql:
            return [(1, 10, self.last, "rss", {})]
        if "SELECT 1 FROM queue_jobs" in sql:
            return [(1,)] if self.queue and self.queue[-1][3] == "poll_subscription" else []
        if "SELECT sub.source_id" in sql:
            return [(1, ["Python"], [], None, [])]
        if "SELECT sub.id FROM subscriptions" in sql:
            return [(1,)]
        if "INSERT INTO jobs" in sql:
            key = (params[3], params[2])
            if key in self.jobs:
                return []
            self.jobs[key] = len(self.jobs) + 1
            return [(self.jobs[key],)]
        if "INSERT INTO queue_jobs" in sql:
            if self.fail_enqueue:
                raise RuntimeError("queue unavailable")
            self.queue.append(params)
            return [(len(self.queue), params[1], params[2], params[3], json.loads(params[4]), 0)]
        if "SET last_enqueued_at" in sql:
            self.last = params[0]
        if "SET last_polled_at" in sql:
            self.polled = True
        return []


def test_due_poll_dedupes_and_respects_interval():
    connection = WatchConnection()
    now = datetime.fromisoformat("2026-10-02T06:00:00+05:30")
    assert enqueue_due(connection, now=now, settings=RuntimeSettings()) == 1
    assert enqueue_due(connection, now=now, settings=RuntimeSettings()) == 0
    # Even after the interval, an outstanding poll prevents another item.
    assert enqueue_due(connection, now=now.replace(minute=20), settings=RuntimeSettings()) == 0


def test_watcher_saves_and_hands_off_new_matches_atomically(project_root, monkeypatch):
    listings = [JobListing(**row) for row in json.loads((project_root / "tests/fixtures/worker_listings.json").read_text())]
    connection = WatchConnection()
    board = source(SourceType.RSS, "https://acme.example/feed")
    monkeypatch.setattr(SourceRepository, "get", lambda self, source_id: board)

    class FixtureDispatcher:
        def fetch(self, source, search_filter, *, hours_old):
            assert hours_old == 1
            return listings

    watcher = Watcher(connection, FixtureDispatcher())
    item = QueueItem(1, uuid4(), None, "poll_subscription", {"subscription_id": 1}, 1)
    watcher.poll(item)
    assert connection.jobs and connection.polled
    assert len(connection.queue) == len(connection.jobs)
    for row in connection.queue:
        assert row[3] == "run_job"
        assert json.loads(row[4])["lane"] == "fast_lane"
    watcher.poll(item)
    assert len(connection.queue) == len(connection.jobs)
    connection.jobs.clear()
    connection.queue.clear()
    connection.fail_enqueue = True
    with pytest.raises(RuntimeError, match="queue unavailable"):
        watcher.poll(item)
    assert not connection.jobs and not connection.queue


def test_jobspy_watcher_uses_posted_within_hour():
    subject, _, _, jobspy = dispatcher()
    board = source(SourceType.JOB_BOARD, "https://indeed.com", adapter="jobspy", site_name="indeed")
    from app.sources.scraper import SearchFilter
    subject.fetch(board, SearchFilter(roles=("Python",)), hours_old=1)
    assert jobspy.calls[0][1]["hours_old"] == 1
