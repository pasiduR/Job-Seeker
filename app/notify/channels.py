"""Push channels: Telegram (with Approve / Reject buttons) and ntfy (link only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from app.http import HttpClient, HttpRequestError, HttpStatusError
from app.triggers.review import Decision


TELEGRAM_API = "https://api.telegram.org"
MAX_TEXT = 3500


class ChannelError(RuntimeError):
    """Sending failed; the message never includes tokens or URLs with secrets."""


@dataclass(frozen=True)
class PushMessage:
    job_id: int
    title: str
    body: str
    link: str | None
    with_decision_buttons: bool


@dataclass(frozen=True)
class DecisionTap:
    update_id: int
    job_id: int
    decision: Decision
    callback_id: str
    message_id: int | None


class Channel(Protocol):
    def send(self, message: PushMessage) -> None: ...


class TelegramChannel:
    def __init__(self, http: HttpClient, *, token: str, chat_id: str) -> None:
        self._http = http
        self._token = token
        self._chat_id = chat_id

    def send(self, message: PushMessage) -> None:
        text = f"{message.title}\n\n{message.body}"
        if message.link:
            text += f"\n\nReview: {message.link}"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text[:MAX_TEXT],
            "disable_web_page_preview": True,
        }
        if message.with_decision_buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "Approve", "callback_data": f"{Decision.APPROVE.value}:{message.job_id}"},
                    {"text": "Reject", "callback_data": f"{Decision.REJECT.value}:{message.job_id}"},
                ]]
            }
        self._call("sendMessage", payload)

    def poll(
        self, *, offset: int | None, timeout_seconds: int
    ) -> tuple[int | None, list[DecisionTap]]:
        """Button taps from the configured chat, plus the next update offset.

        Taps from other chats or with unexpected data are skipped, but the
        offset still moves past them so Telegram does not resend them.
        """

        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        updates = self._call("getUpdates", params).get("result", [])
        taps = [tap for tap in (self._tap(update) for update in updates) if tap is not None]
        ids = [update["update_id"] for update in updates if isinstance(update.get("update_id"), int)]
        return (max(ids) + 1 if ids else offset), taps

    def acknowledge(self, tap: DecisionTap, text: str) -> None:
        if tap.callback_id:
            self._call("answerCallbackQuery", {"callback_query_id": tap.callback_id, "text": text})
        if tap.message_id is not None:
            self._call(
                "editMessageReplyMarkup",
                {"chat_id": self._chat_id, "message_id": tap.message_id, "reply_markup": {"inline_keyboard": []}},
            )

    def _tap(self, update: dict[str, Any]) -> DecisionTap | None:
        callback = update.get("callback_query") or {}
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not callback or chat_id != self._chat_id:
            return None
        action, _, job = str(callback.get("data", "")).partition(":")
        if action not in {d.value for d in Decision} or not job.isdigit():
            return None
        return DecisionTap(
            update_id=int(update["update_id"]),
            job_id=int(job),
            decision=Decision(action),
            callback_id=str(callback.get("id", "")),
            message_id=message.get("message_id"),
        )

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.request(
                "telegram",
                "POST",
                f"{TELEGRAM_API}/bot{self._token}/{method}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode("utf-8"),
            )
        except HttpStatusError as exc:
            raise ChannelError(f"Telegram {method} failed: HTTP {exc.response.status}") from None
        except HttpRequestError:
            raise ChannelError(f"Telegram {method} failed: network error") from None
        result = response.json()
        if not isinstance(result, dict) or not result.get("ok"):
            raise ChannelError(f"Telegram {method} failed")
        return result


class NtfyChannel:
    """ntfy has no safe way to call back into the dashboard, so buttons open it."""

    def __init__(self, http: HttpClient, *, url: str, token: str | None) -> None:
        self._http = http
        self._url = url
        self._token = token

    def send(self, message: PushMessage) -> None:
        headers = {"Title": _header_safe(message.title), "Tags": "briefcase"}
        if message.link:
            headers["Click"] = message.link
            headers["Actions"] = f"view, Review, {message.link}"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            self._http.request(
                "ntfy", "POST", self._url, headers=headers, body=message.body.encode("utf-8")
            )
        except HttpStatusError as exc:
            raise ChannelError(f"ntfy failed: HTTP {exc.response.status}") from None
        except HttpRequestError:
            raise ChannelError("ntfy failed: network error") from None


def _header_safe(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii").replace("\n", " ")[:200]
