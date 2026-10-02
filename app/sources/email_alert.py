"""LinkedIn job-alert ingestion through IMAP without scraping LinkedIn."""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from time import sleep
from urllib.parse import parse_qs, unquote, urlsplit


_LINKEDIN_JOB_PATH = re.compile(r"/(?:comm/)?jobs/view/(\d+)(?:/|$)")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class LinkedInAlertLink:
    job_id: str
    url: str
    subject: str
    received_at: datetime | None


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def parse_linkedin_alert(raw_message: bytes) -> list[LinkedInAlertLink]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = parseaddr(message.get("From", ""))[1].lower()
    domain = sender.rpartition("@")[2]
    if domain != "linkedin.com" and not domain.endswith(".linkedin.com"):
        return []

    subject = str(message.get("Subject", ""))
    received_at = _message_date(message)
    candidates: list[str] = []
    for content_type, text in _message_bodies(message):
        candidates.extend(_URL_PATTERN.findall(text))
        if content_type == "text/html":
            extractor = _LinkExtractor()
            extractor.feed(text)
            candidates.extend(extractor.links)

    links: dict[str, LinkedInAlertLink] = {}
    for candidate in candidates:
        normalized = _linkedin_job_url(candidate)
        if normalized is None:
            continue
        job_id, url = normalized
        links.setdefault(
            job_id,
            LinkedInAlertLink(
                job_id=job_id,
                url=url,
                subject=subject,
                received_at=received_at,
            ),
        )
    return list(links.values())


ImapFactory = Callable[..., imaplib.IMAP4_SSL]


class ImapAlertFetcher:
    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        timeout_seconds: float,
        imap_factory: ImapFactory = imaplib.IMAP4_SSL,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._timeout_seconds = timeout_seconds
        self._imap_factory = imap_factory
        self._sleep = sleeper

    def fetch_unseen(self, mailbox: str = "INBOX") -> list[bytes]:
        for attempt in range(1, 4):
            try:
                return self._fetch_once(mailbox)
            except (OSError, imaplib.IMAP4.error):
                if attempt == 3:
                    raise
                self._sleep(float(2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    def _fetch_once(self, mailbox: str) -> list[bytes]:
        client = self._imap_factory(self._host, timeout=self._timeout_seconds)
        try:
            client.login(self._username, self._password)
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot select mailbox {mailbox!r}")
            status, data = client.search(
                None, '(UNSEEN FROM "jobalerts-noreply@linkedin.com")'
            )
            if status != "OK" or not data:
                raise imaplib.IMAP4.error("LinkedIn alert search failed")

            messages: list[bytes] = []
            for message_id in data[0].split():
                status, fetched = client.fetch(message_id, "(RFC822)")
                if status != "OK":
                    raise imaplib.IMAP4.error(
                        f"failed to fetch message {message_id!r}"
                    )
                messages.extend(_raw_messages(fetched))
            return messages
        finally:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def links_from_alerts(messages: Iterable[bytes]) -> list[LinkedInAlertLink]:
    deduplicated: dict[str, LinkedInAlertLink] = {}
    for message in messages:
        for link in parse_linkedin_alert(message):
            deduplicated.setdefault(link.job_id, link)
    return list(deduplicated.values())


def _message_bodies(message: Message) -> Iterable[tuple[str, str]]:
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if isinstance(content, str):
            yield content_type, content


def _message_date(message: Message) -> datetime | None:
    value = message.get("Date")
    if value is None:
        return None
    try:
        return parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None


def _linkedin_job_url(candidate: str, depth: int = 0) -> tuple[str, str] | None:
    if depth > 1:
        return None
    parts = urlsplit(candidate.rstrip(".,);"))
    hostname = (parts.hostname or "").lower()
    if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
        match = _LINKEDIN_JOB_PATH.search(parts.path)
        if match:
            job_id = match.group(1)
            return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"

        for values in parse_qs(parts.query).values():
            for value in values:
                nested = _linkedin_job_url(unquote(value), depth + 1)
                if nested is not None:
                    return nested
    return None


def _raw_messages(fetched: object) -> list[bytes]:
    if not isinstance(fetched, list):
        return []
    return [
        item[1]
        for item in fetched
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)
    ]
