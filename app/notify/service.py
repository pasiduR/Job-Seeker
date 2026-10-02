"""Deliver pending notifications and turn button taps into review decisions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from app.notify.channels import Channel, ChannelError, DecisionTap, PushMessage, TelegramChannel
from app.triggers.review import Decision, ReviewDecisions


MAX_ATTEMPTS = 3
REVIEW_KIND = "review"
APPLY_MANUALLY_KIND = "apply_manually"


@dataclass(frozen=True)
class PendingNotification:
    id: int
    job_id: int | None
    payload: dict[str, Any]
    attempts: int
    title: str
    company: str
    score: int | None


class NotificationStore(Protocol):
    def pending(self, *, limit: int) -> list[PendingNotification]: ...

    def mark_sent(self, notification_id: int) -> None: ...

    def mark_attempt_failed(self, notification_id: int, error: str, *, final: bool) -> None: ...


class NotifyConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresNotificationStore:
    def __init__(self, connection: NotifyConnection) -> None:
        self._connection = connection

    def pending(self, *, limit: int) -> list[PendingNotification]:
        with self._connection.transaction():
            rows = list(
                self._connection.execute(
                    """
                    SELECT n.id, n.job_id, n.payload, n.attempts,
                           coalesce(j.title, ''), coalesce(j.company, ''), j.score
                    FROM notifications n
                    LEFT JOIN jobs j ON j.id = n.job_id
                    WHERE n.status = 'pending'
                    ORDER BY n.created_at, n.id
                    LIMIT %s
                    """,
                    (limit,),
                )
            )
        return [
            PendingNotification(
                id=int(row[0]),
                job_id=row[1],
                payload=row[2] if isinstance(row[2], dict) else json.loads(row[2]),
                attempts=int(row[3]),
                title=str(row[4]),
                company=str(row[5]),
                score=row[6],
            )
            for row in rows
        ]

    def mark_sent(self, notification_id: int) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                UPDATE notifications
                SET status = 'sent', sent_at = now(), attempts = attempts + 1, error = NULL
                WHERE id = %s
                """,
                (notification_id,),
            )

    def mark_attempt_failed(self, notification_id: int, error: str, *, final: bool) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                UPDATE notifications
                SET attempts = attempts + 1, error = %s,
                    status = CASE WHEN %s THEN 'failed' ELSE status END
                WHERE id = %s
                """,
                (error, final, notification_id),
            )


def build_message(notification: PendingNotification, dashboard_url: str) -> PushMessage:
    kind = notification.payload.get("kind")
    job_id = notification.job_id or 0
    base = dashboard_url.rstrip("/")
    score = f" · score {notification.score}" if notification.score is not None else ""
    heading = f"{notification.title} — {notification.company}{score}"
    if kind == REVIEW_KIND:
        return PushMessage(
            job_id=job_id,
            title=f"Ready to review: {heading}",
            body="The application form is filled. Approve to submit it, or reject to skip.",
            link=f"{base}/review#job-{job_id}" if base else None,
            with_decision_buttons=True,
        )
    if kind == APPLY_MANUALLY_KIND:
        return PushMessage(
            job_id=job_id,
            title=f"Apply manually: {heading}",
            body=f"This job can't be applied to automatically.\n{notification.payload.get('url', '')}",
            link=None,
            with_decision_buttons=False,
        )
    raise ValueError(f"Unknown notification kind: {kind!r}")


class NotificationService:
    def __init__(
        self,
        *,
        store: NotificationStore,
        channel: Channel,
        decisions: ReviewDecisions,
        dashboard_url: str,
    ) -> None:
        self._store = store
        self._channel = channel
        self._decisions = decisions
        self._dashboard_url = dashboard_url

    def send_pending(self, *, limit: int = 20) -> int:
        """Send what is pending; failures are retried up to MAX_ATTEMPTS times."""

        sent = 0
        for notification in self._store.pending(limit=limit):
            try:
                self._channel.send(build_message(notification, self._dashboard_url))
            except (ChannelError, ValueError) as exc:
                self._store.mark_attempt_failed(
                    notification.id,
                    str(exc),
                    final=notification.attempts + 1 >= MAX_ATTEMPTS,
                )
                continue
            self._store.mark_sent(notification.id)
            sent += 1
        return sent

    def handle_taps(self, taps: Iterable[DecisionTap], telegram: TelegramChannel) -> None:
        for tap in taps:
            result = self._decisions.decide(tap.job_id, tap.decision)
            verb = "Approved" if tap.decision == Decision.APPROVE else "Rejected"
            text = f"{verb}; the worker will handle it." if result.queued else result.message
            try:
                telegram.acknowledge(tap, text)
            except ChannelError:
                pass  # The decision is queued; a missing acknowledgement is cosmetic.
