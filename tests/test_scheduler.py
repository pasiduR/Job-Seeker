import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.triggers.scheduler import ScheduleCreate, cron_matches, enqueue_due
from tests.test_dashboard_pages import AUTH, make_client, post, MemoryDashboardStore


def test_cron_fixtures(project_root: Path):
    for case in json.loads((project_root / "tests/fixtures/schedules.json").read_text()):
        assert cron_matches(case["cron"], datetime.fromisoformat(case["time"])) == case["matches"]


@pytest.mark.parametrize("cron", ["* *", "60 * * * *", "*/0 * * * *", "0 6 * JAN *", "0 6 31-1 * *", "0 6 * * 8"])
def test_invalid_cron(cron):
    with pytest.raises(ValidationError):
        ScheduleCreate(name="daily", cron_expression=cron)


class ScheduleConnection:
    def __init__(self):
        self.last = None
        self.items = []
        self.queries = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        self.queries.append(query)
        if query.startswith("SELECT id, cron_expression"):
            return [(1, "0 6 * * *", None, self.last), (2, "bad", None, None)]
        if "INSERT INTO queue_jobs" in query:
            self.items.append(params)
            return [(1, params[1], None, params[3], json.loads(params[4]), 0)]
        if query.startswith("UPDATE schedules"):
            self.last = params[0]
        return []


def test_due_queue_is_idempotent_and_uses_timezone():
    connection = ScheduleConnection()
    now = datetime.fromisoformat("2026-10-02T00:30:20+00:00")
    assert enqueue_due(connection, now=now, timezone_name="Asia/Colombo") == 1
    assert enqueue_due(connection, now=now, timezone_name="Asia/Colombo") == 0
    assert len(connection.items) == 1
    payload = json.loads(connection.items[0][4])
    assert payload["trigger"] == "schedule" and "fill" in payload["steps"]
    assert "FOR UPDATE SKIP LOCKED" in connection.queries[0]


def test_schedule_single_step_and_no_matching_minute():
    class SingleStep(ScheduleConnection):
        def execute(self, query, params=None):
            if query.startswith("SELECT id, cron_expression"):
                return [(1, "*/10 * * * *", "score", self.last)]
            return super().execute(query, params)
    connection = SingleStep()
    now = datetime.fromisoformat("2026-10-02T06:01:00+05:30")
    assert enqueue_due(connection, now=now, timezone_name="Asia/Colombo") == 0
    assert enqueue_due(connection, now=now.replace(minute=10), timezone_name="Asia/Colombo") == 1
    assert json.loads(connection.items[0][4])["steps"] == ["score"]


def test_failed_schedule_enqueue_does_not_advance_cursor():
    class Failed(ScheduleConnection):
        def execute(self, query, params=None):
            if "INSERT INTO queue_jobs" in query:
                raise RuntimeError("queue unavailable")
            return super().execute(query, params)
    connection = Failed()
    with pytest.raises(RuntimeError):
        enqueue_due(connection, now=datetime.fromisoformat("2026-10-02T06:00:00+05:30"), timezone_name="Asia/Colombo")
    assert connection.last is None


class ScheduleDashboard(MemoryDashboardStore):
    def __init__(self):
        super().__init__({"jobs": [], "skills": [], "run_logs": [], "settings": {}})
        self.schedules = {}

    def list_schedules(self):
        return list(self.schedules.values())

    def create_schedule(self, values):
        self.schedules[1] = {"id": 1, **values.model_dump(), "active": True, "last_enqueued_at": None}
        return True

    def set_schedule_active(self, schedule_id, active):
        self.schedules[schedule_id]["active"] = active
        return True

    def delete_schedule(self, schedule_id):
        return self.schedules.pop(schedule_id, None) is not None


def test_schedule_page_validates_and_manages_rows(tmp_path):
    store = ScheduleDashboard()
    client = make_client(store, tmp_path)
    assert client.get("/schedules").status_code == 401
    assert "Asia/Colombo" in client.get("/schedules", auth=AUTH).text
    assert "error=" in post(client, "/schedules", {"name": "daily", "cron_expression": "bad"}).headers["location"]
    assert not store.schedules
    post(client, "/schedules", {"name": "daily", "cron_expression": "0 6 * * *"})
    assert "daily" in client.get("/schedules", auth=AUTH).text
    post(client, "/schedules/1/active", {"active": "false"})
    assert not store.schedules[1]["active"]
    post(client, "/schedules/1/delete", {})
    assert not store.schedules
