"""Remote-board JSON API and generic RSS/Atom source adapters."""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from xml.etree import ElementTree

from app.http import HttpResponse
from app.sources.parsing import html_to_text, parse_timestamp
from app.sources.types import JobListing


class HttpRequester(Protocol):
    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse: ...


class RemoteBoardSource:
    def __init__(self, http: HttpRequester) -> None:
        self._http = http

    def remotive(self) -> list[JobListing]:
        response = self._http.request(
            "remotive", "GET", "https://remotive.com/api/remote-jobs"
        )
        jobs = _object_list(response.json(), "jobs")
        return [
            JobListing(
                title=_required(job, "title"),
                company=_required(job, "company_name"),
                url=_required(job, "url"),
                description=html_to_text(_required(job, "description")),
                location=_optional_string(job.get("candidate_required_location")),
                remote=True,
                posted_at=parse_timestamp(job.get("publication_date")),
                source_job_id=str(job["id"]),
                source_name="remotive",
            )
            for job in jobs
        ]

    def remote_ok(self) -> list[JobListing]:
        response = self._http.request(
            "remoteok",
            "GET",
            "https://remoteok.com/api",
            headers={"User-Agent": "Job-Seeker/0.1"},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("RemoteOK response must be a list")
        jobs = [job for job in payload if isinstance(job, dict) and job.get("position")]
        return [
            JobListing(
                title=_required(job, "position"),
                company=_required(job, "company"),
                url=_required(job, "url"),
                description=html_to_text(_required(job, "description")),
                location=_optional_string(job.get("location")),
                remote=True,
                posted_at=parse_timestamp(job.get("date")),
                source_job_id=str(job["id"]),
                source_name="remoteok",
            )
            for job in jobs
        ]

    def arbeitnow(self) -> list[JobListing]:
        response = self._http.request(
            "arbeitnow", "GET", "https://www.arbeitnow.com/api/job-board-api"
        )
        jobs = _object_list(response.json(), "data")
        return [
            JobListing(
                title=_required(job, "title"),
                company=_required(job, "company_name"),
                url=_required(job, "url"),
                description=html_to_text(_required(job, "description")),
                location=_optional_string(job.get("location")),
                remote=_optional_bool(job.get("remote")),
                posted_at=parse_timestamp(job.get("created_at")),
                source_job_id=_required(job, "slug"),
                source_name="arbeitnow",
            )
            for job in jobs
        ]

    def feed(self, *, url: str, default_company: str) -> list[JobListing]:
        response = self._http.request(f"rss:{url}", "GET", url)
        return parse_feed(response.body, default_company=default_company)


def parse_feed(content: bytes, *, default_company: str) -> list[JobListing]:
    root = ElementTree.fromstring(content)
    if _local_name(root.tag) == "feed":
        entries = [child for child in root if _local_name(child.tag) == "entry"]
        return [_atom_listing(entry, default_company) for entry in entries]

    items = root.findall("./channel/item")
    return [_rss_listing(item, default_company) for item in items]


def _rss_listing(item: ElementTree.Element, default_company: str) -> JobListing:
    title = _element_text(item, "title", required=True)
    link = _element_text(item, "link", required=True)
    description = _element_text(item, "description", required=True)
    posted = _element_text(item, "pubDate")
    return JobListing(
        title=title,
        company=_element_text(item, "company") or default_company,
        url=link,
        description=html_to_text(description),
        location=_element_text(item, "location"),
        remote=None,
        posted_at=_feed_timestamp(posted),
        source_job_id=_element_text(item, "guid") or link,
        source_name="rss",
    )


def _atom_listing(entry: ElementTree.Element, default_company: str) -> JobListing:
    title = _element_text(entry, "title", required=True)
    description = (
        _element_text(entry, "content")
        or _element_text(entry, "summary", required=True)
    )
    link_element = next(
        (child for child in entry if _local_name(child.tag) == "link"), None
    )
    link = link_element.attrib.get("href") if link_element is not None else None
    if not link:
        raise ValueError("Atom entry is missing its link")
    return JobListing(
        title=title,
        company=_element_text(entry, "company") or default_company,
        url=link,
        description=html_to_text(description),
        location=_element_text(entry, "location"),
        remote=None,
        posted_at=_feed_timestamp(
            _element_text(entry, "published") or _element_text(entry, "updated")
        ),
        source_job_id=_element_text(entry, "id") or link,
        source_name="rss",
    )


def _element_text(
    parent: ElementTree.Element, name: str, required: bool = False
) -> str | None:
    for child in parent:
        if _local_name(child.tag) == name:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    if required:
        raise ValueError(f"Feed entry is missing {name!r}")
    return None


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _feed_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return parse_timestamp(value)


def _object_list(payload: object, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ValueError(f"remote-board response must contain a {key!r} list")
    values = payload[key]
    if not all(isinstance(value, dict) for value in values):
        raise ValueError(f"remote-board {key!r} contains a non-object value")
    return values


def _required(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"remote-board job is missing {key!r}")
    return str(value).strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "1", "yes"}
