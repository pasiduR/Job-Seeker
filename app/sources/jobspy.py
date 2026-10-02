"""Low-volume JobSpy adapter for public job boards other than LinkedIn."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime, time, timezone
from time import sleep
from typing import Any

from app.sources.types import JobListing
from app.http import RateLimiter


JobSpyScraper = Callable[..., object]


class JobSpyError(RuntimeError):
    pass


class JobSpySource:
    def __init__(
        self,
        *,
        scraper: JobSpyScraper | None = None,
        timeout_seconds: float,
        sleeper: Callable[[float], None] = sleep,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._scraper = scraper or _default_scraper
        self._timeout_seconds = timeout_seconds
        self._sleep = sleeper
        self._rate_limiter = rate_limiter

    def search(
        self,
        *,
        site_names: Iterable[str],
        search_term: str,
        location: str | None,
        results_wanted: int,
        country_indeed: str | None = None,
        hours_old: int | None = None,
    ) -> list[JobListing]:
        sites = tuple(site.lower() for site in site_names)
        if not sites:
            raise ValueError("at least one JobSpy site is required")
        if "linkedin" in sites:
            raise ValueError("LinkedIn scraping is prohibited; use email alerts")
        if results_wanted <= 0:
            raise ValueError("results_wanted must be positive")

        kwargs = {
            "site_name": list(sites),
            "search_term": search_term,
            "location": location,
            "results_wanted": results_wanted,
            "country_indeed": country_indeed,
            "hours_old": hours_old,
            "verbose": 0,
        }
        result: object | None = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            if self._rate_limiter is not None:
                for site in sites:
                    self._rate_limiter.wait(f"jobspy:{site}")
            try:
                result = _call_with_timeout(
                    self._scraper, kwargs, self._timeout_seconds
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    raise JobSpyError("JobSpy failed after 3 attempts") from exc
                self._sleep(float(2 ** (attempt - 1)))
        if result is None:
            raise JobSpyError("JobSpy returned no result") from last_error

        records = _records(result)
        return [_normalize_record(record) for record in records]


def _call_with_timeout(
    scraper: JobSpyScraper, kwargs: Mapping[str, object], timeout_seconds: float
) -> object:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobspy")
    future = executor.submit(scraper, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"JobSpy exceeded {timeout_seconds} seconds") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _records(result: object) -> list[Mapping[str, Any]]:
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        values = to_dict(orient="records")
    else:
        values = result
    if not isinstance(values, Iterable):
        raise JobSpyError("JobSpy result is not iterable")
    records = list(values)
    if not all(isinstance(record, Mapping) for record in records):
        raise JobSpyError("JobSpy result contains a non-record value")
    return records


def _normalize_record(record: Mapping[str, Any]) -> JobListing:
    posted_at = _posted_at(_optional(record.get("date_posted")))
    return JobListing(
        title=str(record.get("title") or "").strip(),
        company=str(record.get("company") or "").strip(),
        url=str(record.get("job_url") or "").strip(),
        description=str(record.get("description") or "").strip(),
        location=_string(record.get("location")),
        remote=_boolean(record.get("is_remote")),
        posted_at=posted_at,
        source_job_id=_string(record.get("id")),
        source_name=_string(record.get("site")) or "jobspy",
    )


def _optional(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _string(value: object) -> str | None:
    normalized = _optional(value)
    if normalized is None:
        return None
    text = str(normalized).strip()
    return text or None


def _boolean(value: object) -> bool | None:
    normalized = _optional(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        return normalized
    return str(normalized).lower() in {"true", "1", "yes"}


def _posted_at(value: object | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _default_scraper(**kwargs: object) -> object:
    from jobspy import scrape_jobs

    return scrape_jobs(**kwargs)
