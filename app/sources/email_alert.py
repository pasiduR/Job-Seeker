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
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from app.sources.types import JobListing


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
    if not _from_linkedin(message):
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


_GENERIC_LINK_TEXT = frozenset(
    {
        "",
        "view",
        "view job",
        "view jobs",
        "apply",
        "apply now",
        "easy apply",
        "see all jobs",
    }
)
_DETAIL_SEPARATORS = re.compile(r"\s*[·•|]\s*")


@dataclass(frozen=True)
class LinkedInAlertJob:
    job_id: str
    url: str
    title: str
    company: str
    location: str | None
    received_at: datetime | None


class _CardExtractor(HTMLParser):
    """Flatten alert HTML into ("link", href, text) and ("text", text) segments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[tuple[str, ...]] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = " ".join(" ".join(self._link_text).split())
            self.segments.append(("link", self._href, text))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._link_text.append(data)
        elif data.strip():
            self.segments.append(("text", " ".join(data.split())))


def parse_linkedin_alert_jobs(raw_message: bytes) -> list[LinkedInAlertJob]:
    """Title, company, and location for each job card in a LinkedIn alert email.

    Only the email itself is read; LinkedIn pages are never fetched. Cards
    without a recognisable title and company are skipped.
    """

    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if not _from_linkedin(message):
        return []
    received_at = _message_date(message)
    jobs: dict[str, LinkedInAlertJob] = {}
    # HTML cards are structured; plain text only fills in jobs HTML lacked.
    bodies = sorted(_message_bodies(message), key=lambda body: body[0] != "text/html")
    for content_type, text in bodies:
        cards = _html_cards(text) if content_type == "text/html" else _text_cards(text)
        for job_id, url, title, details in cards:
            company, location = _company_and_location(details)
            if job_id in jobs or not title or company is None:
                continue
            jobs[job_id] = LinkedInAlertJob(
                job_id=job_id,
                url=url,
                title=title,
                company=company,
                location=location,
                received_at=received_at,
            )
    return list(jobs.values())


def alert_job_listing(job: LinkedInAlertJob) -> JobListing:
    location = f" ({job.location})" if job.location else ""
    return JobListing(
        title=job.title,
        company=job.company,
        url=job.url,
        description=(
            f"{job.title} at {job.company}{location}. From a LinkedIn job alert "
            "email; the full description is not fetched."
        ),
        location=job.location,
        remote=("remote" in job.location.lower()) if job.location else None,
        posted_at=job.received_at,
        source_job_id=job.job_id,
        source_name="linkedin_alert",
    )


Card = tuple[str, str, str, list[str]]


def _html_cards(html: str) -> list[Card]:
    extractor = _CardExtractor()
    extractor.feed(html)
    cards: list[Card] = []
    current: Card | None = None
    for segment in extractor.segments:
        if segment[0] == "text":
            if current is not None:
                current[3].append(segment[1])
            continue
        normalized = _linkedin_job_url(segment[1])
        if normalized is None:
            continue
        job_id, url = normalized
        text = segment[2]
        if current is not None and current[0] == job_id:
            continue
        if text.lower() in _GENERIC_LINK_TEXT:
            current = None
            continue
        current = (job_id, url, text, [])
        cards.append(current)
    return cards


def _text_cards(text: str) -> list[Card]:
    cards: list[Card] = []
    pending: list[str] = []
    for line in (raw.strip() for raw in text.splitlines()):
        urls = _URL_PATTERN.findall(line)
        normalized = next(
            (found for found in map(_linkedin_job_url, urls) if found is not None), None
        )
        if normalized is not None:
            # A text card is "title / company / location", directly above its link.
            card = pending[-3:]
            if card:
                cards.append((normalized[0], normalized[1], card[0], card[1:]))
            pending = []
        elif urls or set(line) <= {"-", "=", "_"}:
            pending = []
        elif line:
            pending.append(line)
    return cards


def _company_and_location(details: list[str]) -> tuple[str | None, str | None]:
    parts = [
        part
        for detail in details
        for part in _DETAIL_SEPARATORS.split(detail)
        if part.strip()
    ]
    if not parts:
        return None, None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def _from_linkedin(message: Message) -> bool:
    sender = parseaddr(message.get("From", ""))[1].lower()
    domain = sender.rpartition("@")[2]
    return domain == "linkedin.com" or domain.endswith(".linkedin.com")


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


class AlertMailbox(Protocol):
    def fetch_unseen(self, mailbox: str = "INBOX") -> list[bytes]: ...


class EmailAlertSource:
    def __init__(self, mailbox: AlertMailbox) -> None:
        self._mailbox = mailbox

    def listings(self) -> list[JobListing]:
        jobs: dict[str, LinkedInAlertJob] = {}
        for message in self._mailbox.fetch_unseen():
            for job in parse_linkedin_alert_jobs(message):
                jobs.setdefault(job.job_id, job)
        return [alert_job_listing(job) for job in jobs.values()]
