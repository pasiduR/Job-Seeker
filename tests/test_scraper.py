import json
from pathlib import Path

from app.sources.scraper import ScraperService, SearchFilter, canonicalize_job_url
from app.sources.types import JobListing


class FixtureJobStore:
    def __init__(self) -> None:
        self.saved: list[tuple[int, JobListing]] = []

    def save_found(self, source_id: int, listing: JobListing) -> bool:
        self.saved.append((source_id, listing))
        return True


def test_scraper_filters_deduplicates_and_saves_found_jobs(
    project_root: Path,
) -> None:
    fixture = json.loads(
        (project_root / "tests/fixtures/scraped_jobs.json").read_text(
            encoding="utf-8"
        )
    )
    listings = [JobListing.model_validate(value) for value in fixture]
    store = FixtureJobStore()
    scraper = ScraperService(store)

    result = scraper.save_matches(
        source_id=9,
        listings=listings,
        search_filter=SearchFilter(
            roles=("Python",),
            locations=("Remote",),
            remote=True,
            exclude_keywords=("staffing agency",),
        ),
    )

    assert result.seen == 5
    assert result.filtered == 2
    assert result.duplicates == 2
    assert result.saved == 1
    assert store.saved[0][0] == 9
    assert store.saved[0][1].url == "https://jobs.example.com/python"


def test_job_url_canonicalization_removes_only_tracking_parameters() -> None:
    assert canonicalize_job_url(
        "HTTPS://Jobs.Example.com:443/role/?id=7&utm_campaign=test&trackingId=x#apply"
    ) == "https://jobs.example.com/role?id=7"
