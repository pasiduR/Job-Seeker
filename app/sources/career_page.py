"""Playwright career-page fetch and structured LLM listing extraction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, time, timezone
from pathlib import Path
from time import sleep
from typing import Protocol
from urllib.parse import urljoin

from app.llm.schemas import ListingExtractorOutput
from app.sources.types import JobListing


PROMPT_VERSION = "listing_extract_v1"
PROMPT_PATH = (
    Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"
)


class PageFetcher(Protocol):
    def fetch_text(self, url: str) -> str: ...


class ListingExtractorClient(Protocol):
    def generate(
        self,
        *,
        schema: type[ListingExtractorOutput],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> ListingExtractorOutput: ...


class PlaywrightPageFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        headless: bool = True,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_ms = round(timeout_seconds * 1000)
        self._headless = headless
        self._sleep = sleeper

    def fetch_text(self, url: str) -> str:
        for attempt in range(1, 4):
            try:
                return self._fetch_once(url)
            except Exception:
                if attempt == 3:
                    raise
                self._sleep(float(2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    def _fetch_once(self, url: str) -> str:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self._headless)
            try:
                page = browser.new_page()
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                return page.locator("body").inner_text(timeout=self._timeout_ms)
            finally:
                browser.close()


class CareerPageSource:
    def __init__(
        self,
        *,
        fetcher: PageFetcher,
        llm: ListingExtractorClient,
        model: str,
    ) -> None:
        self._fetcher = fetcher
        self._llm = llm
        self._model = model

    def discover(self, url: str) -> list[JobListing]:
        page_text = self._fetcher.fetch_text(url)
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        extracted = self._llm.generate(
            schema=ListingExtractorOutput,
            job_id=None,
            step="listing_extractor",
            prompt_version=PROMPT_VERSION,
            model=self._model,
            prompt=prompt,
            untrusted_data={"career_page": page_text},
        )
        return [
            JobListing(
                title=listing.title,
                company=listing.company,
                url=urljoin(url, listing.url),
                description=listing.description,
                location=listing.location,
                remote=(
                    "remote" in listing.location.lower()
                    if listing.location is not None
                    else None
                ),
                posted_at=(
                    datetime.combine(listing.posted_date, time.min, tzinfo=timezone.utc)
                    if listing.posted_date is not None
                    else None
                ),
                source_job_id=None,
                source_name="career_page",
            )
            for listing in extracted.listings
        ]
