"""Public job-board API adapters for Greenhouse, Lever, and Ashby."""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

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


class AtsApiSource:
    def __init__(self, http: HttpRequester) -> None:
        self._http = http

    def greenhouse(self, *, board_token: str, company: str) -> list[JobListing]:
        board = quote(board_token, safe="")
        response = self._http.request(
            f"greenhouse:{board_token}",
            "GET",
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
        )
        payload = response.json()
        jobs = _require_list(payload, "jobs")
        return [
            JobListing(
                title=_required(job, "title"),
                company=company,
                url=_required(job, "absolute_url"),
                description=html_to_text(_required(job, "content")),
                location=_nested_string(job, "location", "name"),
                remote=_remote_from_location(_nested_string(job, "location", "name")),
                posted_at=parse_timestamp(
                    job.get("first_published") or job.get("updated_at")
                ),
                source_job_id=str(job["id"]),
                source_name="greenhouse",
            )
            for job in jobs
        ]

    def lever(self, *, site: str, company: str) -> list[JobListing]:
        site_token = quote(site, safe="")
        response = self._http.request(
            f"lever:{site}",
            "GET",
            f"https://api.lever.co/v0/postings/{site_token}?mode=json",
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Lever response must be a list")
        return [
            JobListing(
                title=_required(job, "text"),
                company=company,
                url=_required(job, "hostedUrl"),
                description=_required(job, "descriptionPlain"),
                location=_nested_string(job, "categories", "location"),
                remote=(
                    str(job.get("workplaceType", "")).lower() == "remote"
                    or _remote_from_location(
                        _nested_string(job, "categories", "location")
                    )
                ),
                posted_at=parse_timestamp(job.get("createdAt")),
                source_job_id=_required(job, "id"),
                source_name="lever",
            )
            for job in payload
        ]

    def ashby(self, *, board_name: str, company: str) -> list[JobListing]:
        board = quote(board_name, safe="")
        response = self._http.request(
            f"ashby:{board_name}",
            "GET",
            f"https://api.ashbyhq.com/posting-api/job-board/{board}",
        )
        payload = response.json()
        jobs = _require_list(payload, "jobs")
        return [
            JobListing(
                title=_required(job, "title"),
                company=company,
                url=_required(job, "jobUrl"),
                description=_required(job, "descriptionPlain"),
                location=_optional_string(job.get("location")),
                remote=_optional_bool(job.get("isRemote")),
                posted_at=parse_timestamp(job.get("publishedAt")),
                source_job_id=_required(job, "id"),
                source_name="ashby",
            )
            for job in jobs
        ]


def _require_list(payload: object, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ValueError(f"ATS response must contain a {key!r} list")
    values = payload[key]
    if not all(isinstance(value, dict) for value in values):
        raise ValueError(f"ATS {key!r} contains a non-object value")
    return values


def _required(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"ATS job is missing {key!r}")
    return str(value).strip()


def _nested_string(record: dict[str, Any], parent: str, key: str) -> str | None:
    value = record.get(parent)
    if not isinstance(value, dict):
        return None
    return _optional_string(value.get(key))


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


def _remote_from_location(location: str | None) -> bool | None:
    if location is None:
        return None
    return "remote" in location.lower()
