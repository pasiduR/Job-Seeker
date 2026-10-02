from datetime import date
from pathlib import Path
from typing import Any

from app.llm.schemas import ExtractedListing, ListingExtractorOutput
from app.sources.career_page import CareerPageSource, PROMPT_VERSION


class FixtureFetcher:
    def __init__(self, text: str) -> None:
        self.text = text
        self.urls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.urls.append(url)
        return self.text


class FixtureExtractor:
    def __init__(self) -> None:
        self.call: dict[str, Any] | None = None

    def generate(self, **kwargs: Any) -> ListingExtractorOutput:
        self.call = kwargs
        return ListingExtractorOutput(
            listings=[
                ExtractedListing(
                    title="Python Engineer",
                    company="Example Co",
                    url="/jobs/python-engineer",
                    description="Build reliable Python services for customers.",
                    location="Remote",
                    posted_date=date(2026, 10, 1),
                )
            ]
        )


def test_career_page_fetches_and_extracts_fixture_text(project_root: Path) -> None:
    page_text = (project_root / "tests/fixtures/career_page.txt").read_text(
        encoding="utf-8"
    )
    fetcher = FixtureFetcher(page_text)
    extractor = FixtureExtractor()
    source = CareerPageSource(
        fetcher=fetcher,
        llm=extractor,
        model="fixture-model",
    )

    listings = source.discover("https://example.com/careers")

    assert fetcher.urls == ["https://example.com/careers"]
    assert listings[0].url == "https://example.com/jobs/python-engineer"
    assert listings[0].remote is True
    assert listings[0].posted_at is not None
    assert extractor.call is not None
    assert extractor.call["prompt_version"] == PROMPT_VERSION
    assert extractor.call["untrusted_data"] == {"career_page": page_text}
    assert "Return JSON" in extractor.call["prompt"]
