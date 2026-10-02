import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.http import HttpClient, HttpRequest, HttpResponse
from app.notify.channels import ChannelError, NtfyChannel, PushMessage, TelegramChannel
from app.notify.service import (
    NotificationService,
    PendingNotification,
    PostgresNotificationStore,
    build_message,
)
from app.triggers.review import REVIEW_DECISION_TASK, Decision, ReviewDecisions
from tests.test_manual_trigger import MemoryTriggerQueue


TOKEN = "123456:SECRET-TOKEN"
CHAT = "424242"
RUN_ID = UUID("6f0c8b9e-1d2a-4c3b-8e7f-5a6b7c8d9e0f")


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest, timeout: float) -> HttpResponse:
        self.requests.append(request)
        return next(self.responses)

    def body(self, index: int) -> dict[str, Any]:
        raw = self.requests[index].body
        assert raw is not None
        return json.loads(raw)


def ok(result: Any = True) -> HttpResponse:
    return HttpResponse(status=200, headers={}, body=json.dumps({"ok": True, "result": result}).encode())


def http(transport: FakeTransport) -> HttpClient:
    return HttpClient(timeout_seconds=5, source_minimum_intervals={}, transport=transport, sleeper=lambda _: None)


def pending(kind: str, **payload: Any) -> PendingNotification:
    return PendingNotification(
        id=1, job_id=12, payload={"kind": kind, **payload}, attempts=0,
        title="Backend Engineer", company="Northwind", score=8,
    )


def test_review_message_has_score_link_and_buttons() -> None:
    message = build_message(pending("review"), "https://dash.example.com/")

    assert message.title == "Ready to review: Backend Engineer — Northwind · score 8"
    assert message.link == "https://dash.example.com/review#job-12"
    assert message.with_decision_buttons is True


def test_apply_manually_message_has_no_buttons_and_no_link_without_dashboard_url() -> None:
    message = build_message(pending("apply_manually", url="https://www.linkedin.com/jobs/view/1"), "")

    assert message.title.startswith("Apply manually: Backend Engineer")
    assert "https://www.linkedin.com/jobs/view/1" in message.body
    assert (message.link, message.with_decision_buttons) == (None, False)


def test_telegram_send_includes_approve_and_reject_buttons() -> None:
    transport = FakeTransport([ok({"message_id": 1})])
    message = PushMessage(job_id=12, title="Ready", body="Filled", link="https://d/review#job-12", with_decision_buttons=True)

    TelegramChannel(http(transport), token=TOKEN, chat_id=CHAT).send(message)

    assert transport.requests[0].url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = transport.body(0)
    assert body["chat_id"] == CHAT and "Review: https://d/review#job-12" in body["text"]
    assert body["reply_markup"]["inline_keyboard"] == [[
        {"text": "Approve", "callback_data": "approve:12"},
        {"text": "Reject", "callback_data": "reject:12"},
    ]]


def test_telegram_errors_never_include_the_token() -> None:
    transport = FakeTransport([HttpResponse(status=403, headers={}, body=b'{"ok": false}')])

    with pytest.raises(ChannelError) as error:
        TelegramChannel(http(transport), token=TOKEN, chat_id=CHAT).send(
            PushMessage(1, "t", "b", None, False)
        )

    assert str(error.value) == "Telegram sendMessage failed: HTTP 403"
    assert TOKEN not in repr(error.value) and error.value.__cause__ is None


def test_telegram_poll_only_accepts_taps_from_the_configured_chat(project_root: Path) -> None:
    updates = (project_root / "tests/fixtures/telegram_updates.json").read_bytes()
    transport = FakeTransport([HttpResponse(status=200, headers={}, body=updates)])

    offset, taps = TelegramChannel(http(transport), token=TOKEN, chat_id=CHAT).poll(
        offset=500, timeout_seconds=25
    )

    assert offset == 505
    assert [(tap.job_id, tap.decision) for tap in taps] == [(12, Decision.APPROVE), (14, Decision.REJECT)]
    assert transport.body(0) == {"timeout": 25, "allowed_updates": ["callback_query"], "offset": 500}


def test_ntfy_send_uses_a_view_action_and_bearer_token() -> None:
    transport = FakeTransport([HttpResponse(status=200, headers={}, body=b"{}")])
    message = PushMessage(12, "Ready — Café Ltd", "Filled", "https://d/review#job-12", True)

    NtfyChannel(http(transport), url="https://ntfy.sh/my-topic", token="tk").send(message)

    request = transport.requests[0]
    assert request.url == "https://ntfy.sh/my-topic"
    assert request.headers["Actions"] == "view, Review, https://d/review#job-12"
    assert request.headers["Authorization"] == "Bearer tk"
    assert request.headers["Title"] == "Ready ? Caf? Ltd"  # headers stay ASCII


class MemoryNotificationStore:
    def __init__(self, items: list[PendingNotification]) -> None:
        self.items = items
        self.sent: list[int] = []
        self.failures: list[tuple[int, str, bool]] = []

    def pending(self, *, limit: int) -> list[PendingNotification]:
        return self.items[:limit]

    def mark_sent(self, notification_id: int) -> None:
        self.sent.append(notification_id)

    def mark_attempt_failed(self, notification_id: int, error: str, *, final: bool) -> None:
        self.failures.append((notification_id, error, final))


class FlakyChannel:
    def __init__(self, fail_ids: set[int]) -> None:
        self.fail_ids = fail_ids
        self.messages: list[PushMessage] = []

    def send(self, message: PushMessage) -> None:
        if message.job_id in self.fail_ids:
            raise ChannelError("ntfy failed: HTTP 500")
        self.messages.append(message)


def test_send_pending_marks_sent_and_counts_failed_attempts() -> None:
    items = [
        pending("review"),
        PendingNotification(2, 13, {"kind": "review"}, 0, "A", "B", None),
        PendingNotification(3, 14, {"kind": "review"}, 2, "C", "D", None),
        PendingNotification(4, 15, {"kind": "mystery"}, 0, "E", "F", None),
    ]
    store = MemoryNotificationStore(items)
    service = NotificationService(
        store=store, channel=FlakyChannel({13, 14}), decisions=ReviewDecisions(MemoryTriggerQueue()), dashboard_url=""
    )

    assert service.send_pending() == 1
    assert store.sent == [1]
    assert store.failures == [
        (2, "ntfy failed: HTTP 500", False),
        (3, "ntfy failed: HTTP 500", True),  # third attempt is final
        (4, "Unknown notification kind: 'mystery'", False),
    ]


def test_button_taps_queue_review_decisions_once(project_root: Path) -> None:
    updates = (project_root / "tests/fixtures/telegram_updates.json").read_bytes()
    transport = FakeTransport([HttpResponse(200, {}, updates)] + [ok()] * 8)
    telegram = TelegramChannel(http(transport), token=TOKEN, chat_id=CHAT)
    queue = MemoryTriggerQueue()
    service = NotificationService(
        store=MemoryNotificationStore([]), channel=telegram, decisions=ReviewDecisions(queue), dashboard_url=""
    )

    _, taps = telegram.poll(offset=None, timeout_seconds=1)
    service.handle_taps(taps + taps[:1], telegram)  # the approve tap arrives twice

    assert [(item["task"], item["job_id"], item["payload"]) for item in queue.items] == [
        (REVIEW_DECISION_TASK, 12, {"decision": "approve", "trigger": "manual"}),
        (REVIEW_DECISION_TASK, 14, {"decision": "reject", "trigger": "manual"}),
    ]
    answers = [transport.body(i)["text"] for i, r in enumerate(transport.requests) if r.url.endswith("answerCallbackQuery")]
    assert answers == [
        "Approved; the worker will handle it.",
        "Rejected; the worker will handle it.",
        "Approve for job 12 is already queued",
    ]


def test_review_decisions_use_the_review_task() -> None:
    queue = MemoryTriggerQueue()
    result = ReviewDecisions(queue, new_run_id=lambda: RUN_ID).decide(5, Decision.REJECT)

    assert result.queued and result.message == "Reject for job 5 queued"
    assert queue.items[0]["idempotency_key"] == f"review:5:reject:{RUN_ID}"


class ScriptedConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[Any]:
        self.queries.append((" ".join(query.split()), params))
        return [(1, 12, {"kind": "review"}, 0, "Backend Engineer", "Northwind", 8)]


def test_postgres_store_reads_pending_and_finalizes_failures() -> None:
    connection = ScriptedConnection()
    store = PostgresNotificationStore(connection)

    [item] = store.pending(limit=5)
    store.mark_attempt_failed(1, "boom", final=True)

    assert item.payload == {"kind": "review"} and item.score == 8
    assert "WHERE n.status = 'pending'" in connection.queries[0][0]
    assert connection.queries[1][1] == ("boom", True, 1)


def test_build_channel_prefers_telegram_then_ntfy() -> None:
    from app.config import SecretSettings
    from app.notify.__main__ import build_channel

    both = SecretSettings(_env_file=None, telegram_bot_token=TOKEN, telegram_chat_id=CHAT, ntfy_url="https://ntfy.sh/t")
    ntfy_only = SecretSettings(_env_file=None, ntfy_url="https://ntfy.sh/t")
    none = SecretSettings(_env_file=None)

    assert isinstance(build_channel(both), TelegramChannel)
    assert isinstance(build_channel(ntfy_only), NtfyChannel)
    assert build_channel(none) is None
