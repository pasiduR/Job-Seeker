"""Route each configured source to the adapter for its type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

from app.sources.models import Source, SourceType
from app.sources.scraper import SearchFilter
from app.sources.types import JobListing


class UnsupportedSource(ValueError):
    """The source's type or config has no adapter yet."""


class JobSpySearch(Protocol):
    def search(
        self,
        *,
        site_names: list[str],
        search_term: str,
        location: str | None,
        results_wanted: int,
        country_indeed: str | None = None,
        hours_old: int | None = None,
    ) -> list[JobListing]: ...


class RemoteBoards(Protocol):
    def remotive(self) -> list[JobListing]: ...

    def remote_ok(self) -> list[JobListing]: ...

    def arbeitnow(self) -> list[JobListing]: ...

    def feed(self, *, url: str, default_company: str) -> list[JobListing]: ...


class AtsBoards(Protocol):
    def greenhouse(self, *, board_token: str, company: str) -> list[JobListing]: ...

    def lever(self, *, site: str, company: str) -> list[JobListing]: ...

    def ashby(self, *, board_name: str, company: str) -> list[JobListing]: ...


class EmailAlerts(Protocol):
    def listings(self) -> list[JobListing]: ...


class CareerPages(Protocol):
    def discover(self, url: str) -> list[JobListing]: ...


_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}


def ats_target(source: Source) -> tuple[str, str]:
    """(provider, board) from explicit config, else from the board URL."""

    return ats_target_from(source.url, source.config, name=source.name)


def ats_target_from(
    url: str, config: Mapping[str, object], *, name: str
) -> tuple[str, str]:
    provider = config.get("provider")
    board = config.get("board")
    if isinstance(provider, str) and isinstance(board, str) and board:
        return provider, board
    parts = urlsplit(url)
    inferred = _ATS_HOSTS.get(parts.hostname or "")
    segments = [segment for segment in parts.path.split("/") if segment]
    if inferred is None or not segments:
        raise UnsupportedSource(
            f"Cannot determine ATS provider and board for source {name!r}; "
            'set config {"provider": ..., "board": ...}'
        )
    return inferred, segments[0]


def depends_on_filter(source: Source) -> bool:
    """JobSpy searches by role and location; every other adapter lists everything."""

    return source.type == SourceType.JOB_BOARD and source.config.get("adapter") == "jobspy"


class SourceDispatcher:
    def __init__(
        self,
        *,
        boards: RemoteBoards,
        ats: AtsBoards,
        jobspy: JobSpySearch | None = None,
        career_pages: CareerPages | None = None,
        email_alerts: EmailAlerts | None = None,
        jobspy_results_wanted: int,
    ) -> None:
        self._boards = boards
        self._ats = ats
        self._jobspy = jobspy
        self._career_pages = career_pages
        self._email_alerts = email_alerts
        self._jobspy_results_wanted = jobspy_results_wanted

    def fetch(self, source: Source, search_filter: SearchFilter) -> list[JobListing]:
        company = str(source.config.get("company") or source.name)
        if source.type == SourceType.JOB_BOARD:
            return self._job_board(source, search_filter)
        if source.type == SourceType.ATS_BOARD:
            provider, board = ats_target(source)
            if provider == "greenhouse":
                return self._ats.greenhouse(board_token=board, company=company)
            if provider == "lever":
                return self._ats.lever(site=board, company=company)
            if provider == "ashby":
                return self._ats.ashby(board_name=board, company=company)
            raise UnsupportedSource(f"Unknown ATS provider {provider!r}")
        if source.type == SourceType.RSS:
            return self._boards.feed(url=source.url, default_company=company)
        if source.type == SourceType.CAREER_PAGE:
            if self._career_pages is None:
                raise UnsupportedSource("Career pages need the LLM listing extractor")
            return self._career_pages.discover(source.url)
        if source.type == SourceType.EMAIL_ALERT:
            if self._email_alerts is None:
                raise UnsupportedSource("Email alerts need IMAP settings in .env")
            return self._email_alerts.listings()
        raise UnsupportedSource(f"Unknown source type {source.type.value!r}")

    def _job_board(self, source: Source, search_filter: SearchFilter) -> list[JobListing]:
        adapter = source.config.get("adapter")
        if adapter == "remotive":
            return self._boards.remotive()
        if adapter == "remoteok":
            return self._boards.remote_ok()
        if adapter == "arbeitnow":
            return self._boards.arbeitnow()
        if adapter != "jobspy":
            raise UnsupportedSource(f"Unknown job board adapter {adapter!r}")
        if self._jobspy is None:
            raise UnsupportedSource("JobSpy is not available")
        site = str(source.config.get("site_name") or "")
        if not site:
            raise UnsupportedSource(f"JobSpy source {source.name!r} needs config.site_name")
        if not search_filter.roles:
            raise UnsupportedSource("JobSpy searches need at least one role in the filter")

        listings: list[JobListing] = []
        for role in search_filter.roles:
            for location in search_filter.locations or (None,):
                listings.extend(
                    self._jobspy.search(
                        site_names=[site],
                        search_term=role,
                        location=location,
                        results_wanted=self._jobspy_results_wanted,
                    )
                )
        return listings
