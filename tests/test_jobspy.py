import json
from pathlib import Path

import pytest

from app.sources.jobspy import JobSpySource


def test_jobspy_normalizes_fixture_records_and_passes_low_volume_limit(
    project_root: Path,
) -> None:
    records = json.loads(
        (project_root / "tests/fixtures/jobspy_records.json").read_text(
            encoding="utf-8"
        )
    )
    calls: list[dict[str, object]] = []

    def scraper(**kwargs: object) -> object:
        calls.append(kwargs)
        return records

    source = JobSpySource(
        scraper=scraper,
        timeout_seconds=5,
        sleeper=lambda _: None,
    )

    listings = source.search(
        site_names=["indeed", "glassdoor"],
        search_term="backend engineer",
        location="Sri Lanka",
        results_wanted=10,
        country_indeed="Sri Lanka",
    )

    assert len(listings) == 1
    assert listings[0].source_job_id == "indeed-123"
    assert listings[0].title == "Backend Engineer"
    assert listings[0].posted_at is not None
    assert calls[0]["results_wanted"] == 10
    assert calls[0]["site_name"] == ["indeed", "glassdoor"]


def test_jobspy_refuses_linkedin_scraping() -> None:
    source = JobSpySource(
        scraper=lambda **_: [],
        timeout_seconds=5,
        sleeper=lambda _: None,
    )

    with pytest.raises(ValueError, match="LinkedIn scraping is prohibited"):
        source.search(
            site_names=["linkedin"],
            search_term="python",
            location=None,
            results_wanted=5,
        )
