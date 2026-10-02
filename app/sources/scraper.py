"""Deterministic filtering, dedupe, and persistence for scraped listings."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from app.sources.types import JobListing


_TRACKING_QUERY_KEYS = frozenset(
    {"ref", "refid", "trackingid", "trk", "source", "campaign"}
)


class SearchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[str, ...] = Field(default_factory=tuple)
    locations: tuple[str, ...] = Field(default_factory=tuple)
    remote: bool | None = None
    exclude_keywords: tuple[str, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class ScrapeResult:
    seen: int
    filtered: int
    duplicates: int
    saved: int


class JobStore(Protocol):
    def save_found(self, source_id: int, listing: JobListing) -> bool: ...


class JobConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresJobStore:
    def __init__(self, connection: JobConnection) -> None:
        self._connection = connection

    def save_found(self, source_id: int, listing: JobListing) -> bool:
        return self.save_found_id(source_id, listing) is not None

    def save_found_id(self, source_id: int, listing: JobListing) -> int | None:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                INSERT INTO jobs (
                    source_id, source_job_id, title, company, url, description,
                    location, remote, posted_at, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'found')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    source_id,
                    listing.source_job_id,
                    listing.title,
                    listing.company,
                    canonicalize_job_url(listing.url),
                    listing.description,
                    listing.location,
                    listing.remote,
                    listing.posted_at,
                ),
            )
            row = next(iter(rows), None)
            return int(row[0]) if row is not None else None


class ScraperService:
    def __init__(self, store: JobStore) -> None:
        self._store = store

    def save_matches(
        self,
        *,
        source_id: int,
        listings: Iterable[JobListing],
        search_filter: SearchFilter,
    ) -> ScrapeResult:
        seen = 0
        filtered = 0
        duplicates = 0
        saved = 0
        seen_urls: set[str] = set()
        seen_company_titles: set[tuple[str, str]] = set()

        for listing in listings:
            seen += 1
            if not matches_filter(listing, search_filter):
                filtered += 1
                continue

            canonical_url = canonicalize_job_url(listing.url)
            company_title = (
                listing.company.casefold().strip(),
                listing.title.casefold().strip(),
            )
            if canonical_url in seen_urls or company_title in seen_company_titles:
                duplicates += 1
                continue
            seen_urls.add(canonical_url)
            seen_company_titles.add(company_title)

            canonical_listing = listing.model_copy(update={"url": canonical_url})
            if self._store.save_found(source_id, canonical_listing):
                saved += 1
            else:
                duplicates += 1

        return ScrapeResult(
            seen=seen,
            filtered=filtered,
            duplicates=duplicates,
            saved=saved,
        )


def matches_filter(listing: JobListing, search_filter: SearchFilter) -> bool:
    title = listing.title.casefold()
    location = (listing.location or "").casefold()
    searchable = "\n".join(
        (
            listing.title,
            listing.company,
            listing.description,
            listing.location or "",
        )
    ).casefold()

    if search_filter.roles and not any(
        role.casefold() in title for role in search_filter.roles
    ):
        return False
    if search_filter.locations and not any(
        expected.casefold() in location for expected in search_filter.locations
    ):
        return False
    if search_filter.remote is not None and listing.remote is not search_filter.remote:
        return False
    if any(
        keyword.casefold() in searchable for keyword in search_filter.exclude_keywords
    ):
        return False
    return True


def canonicalize_job_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError("job URL must be absolute")
    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_QUERY_KEYS
        )
    )
    return urlunsplit((scheme, hostname, parts.path.rstrip("/"), query, ""))
