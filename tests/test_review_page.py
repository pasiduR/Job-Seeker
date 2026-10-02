from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from app.dashboard import pages
from app.dashboard.repository import PostgresDashboardStore, ReviewItem
from app.steps.form_mapper import RESUME_UPLOAD
from tests.test_dashboard_pages import AUTH, MemoryDashboardStore, make_client, post, store  # noqa: F401
from tests.test_manual_trigger import MemoryTriggerQueue


def review_item(job_id: int, screenshot: str | None, diff: str | None = "-old\n+new") -> ReviewItem:
    return ReviewItem(
        job_id=job_id,
        title="Backend Engineer",
        company="Northwind",
        url="https://boards.greenhouse.io/northwind/jobs/1",
        score=8,
        answers=[
            {"field_id": "f1", "label": "Email *", "type": "email", "required": True,
             "value": "jane@example.com", "source": "profile", "flag": None},
            {"field_id": "f2", "label": "Resume", "type": "file", "required": True,
             "value": RESUME_UPLOAD, "source": "cv", "flag": None},
            {"field_id": "f3", "label": "Why us?", "type": "textarea", "required": False,
             "value": "I like <b>reliable</b> services.", "source": "drafted", "flag": None},
            {"field_id": "f4", "label": "Visa sponsorship?", "type": "radio_group", "required": True,
             "value": "Yes", "source": "profile",
             "flag": "work_authorization answer from profile; confirm before submitting"},
            {"field_id": "f5", "label": "Agree to terms", "type": "checkbox", "required": False,
             "value": True, "source": "profile", "flag": None},
        ],
        screenshot_path=screenshot,
        cv_diff=diff,
    )


@pytest.fixture
def screenshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "screenshots"
    (root / "job-1").mkdir(parents=True)
    (root / "job-1" / "final.png").write_bytes(b"\x89PNG fixture")
    monkeypatch.setattr(pages, "SCREENSHOT_ROOT", root)
    return root


def test_review_page_shows_answers_flags_diff_and_actions(
    store: MemoryDashboardStore, tmp_path: Path, screenshot_root: Path  # noqa: F811
) -> None:
    store.review_items = [
        review_item(1, str(screenshot_root / "job-1" / "final.png")),
        review_item(2, None, diff=None),
    ]
    page = make_client(store, tmp_path).get("/review", auth=AUTH).text

    assert '<img src="/review/1/screenshot"' in page
    assert "2 flagged fields" in page
    assert "Tailored CV (PDF)" in page
    assert "I like &lt;b&gt;reliable&lt;/b&gt; services." in page  # drafted text is escaped
    assert "confirm before submitting" in page and "check drafted text" in page
    assert "<pre>-old\n+new</pre>" in page
    assert "Base CV used unchanged." in page
    assert 'action="/review/1/approve"' in page and 'action="/review/2/reject"' in page
    assert ">Yes<" in page  # booleans read as Yes/No


def test_screenshots_are_served_only_from_the_screenshot_directory(
    store: MemoryDashboardStore, tmp_path: Path, screenshot_root: Path  # noqa: F811
) -> None:
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    store.review_items = [
        review_item(1, str(screenshot_root / "job-1" / "final.png")),
        review_item(2, str(outside)),
        review_item(3, str(screenshot_root / "job-1" / ".." / ".." / "secret.png")),
    ]
    client = make_client(store, tmp_path)

    ok = client.get("/review/1/screenshot", auth=AUTH)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    assert client.get("/review/2/screenshot", auth=AUTH).status_code == 404
    assert client.get("/review/3/screenshot", auth=AUTH).status_code == 404
    assert client.get("/review/9/screenshot", auth=AUTH).status_code == 404


class ScriptedConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[tuple[Any, ...]]:
        self.queries.append(" ".join(query.split()))
        return self.rows


def test_postgres_review_items_only_include_filled_jobs() -> None:
    connection = ScriptedConnection(
        [(1, "Backend Engineer", "Northwind", "https://x.example", 8, '[{"label": "Email"}]', "/s/1.png", None)]
    )

    [item] = PostgresDashboardStore(connection).list_review_items(limit=5)  # type: ignore[arg-type]

    assert item.answers == [{"label": "Email"}] and item.cv_diff is None
    assert "WHERE j.status = 'filled'" in connection.queries[0]
    assert "f.outcome = 'filled'" in connection.queries[0]


def test_approve_and_reject_buttons_queue_review_decisions(
    store: MemoryDashboardStore, tmp_path: Path  # noqa: F811
) -> None:
    queue = MemoryTriggerQueue()
    client = make_client(store, tmp_path, queue=queue)

    approved = post(client, "/review/4/approve", {})
    repeat = post(client, "/review/4/approve", {})
    rejected = post(client, "/review/5/reject", {})
    unknown = post(client, "/review/5/submit", {})

    assert approved.headers["location"] == "/review?message=Approve+for+job+4+queued"
    assert "already+queued" in repeat.headers["location"]
    assert rejected.headers["location"] == "/review?message=Reject+for+job+5+queued"
    assert unknown.status_code == 422
    assert [(item["task"], item["job_id"], item["payload"]["decision"]) for item in queue.items] == [
        ("review_decision", 4, "approve"),
        ("review_decision", 5, "reject"),
    ]
