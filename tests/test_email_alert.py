from pathlib import Path

from app.sources.email_alert import (
    EmailAlertSource,
    links_from_alerts,
    parse_linkedin_alert,
    parse_linkedin_alert_jobs,
)


def test_linkedin_alert_parser_extracts_canonical_deduplicated_links(
    project_root: Path,
) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert.eml").read_bytes()

    links = parse_linkedin_alert(message)

    assert [link.job_id for link in links] == ["123456", "789012"]
    assert [link.url for link in links] == [
        "https://www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com/jobs/view/789012",
    ]
    assert all(link.subject == "New Python jobs for you" for link in links)
    assert all(link.received_at is not None for link in links)


def test_alert_collection_deduplicates_jobs_across_messages(project_root: Path) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert.eml").read_bytes()

    assert len(links_from_alerts([message, message])) == 2


def test_non_linkedin_sender_is_ignored(project_root: Path) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert.eml").read_bytes()
    message = message.replace(
        b"jobalerts-noreply@linkedin.com", b"attacker@example.com"
    )

    assert parse_linkedin_alert(message) == []


def test_alert_cards_yield_title_company_and_location(project_root: Path) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert_cards.eml").read_bytes()

    jobs = {job.job_id: job for job in parse_linkedin_alert_jobs(message)}

    assert set(jobs) == {"111111", "222222", "333333"}
    assert (jobs["111111"].title, jobs["111111"].company, jobs["111111"].location) == (
        "Python Engineer",
        "Northwind Logistics",
        "Colombo, Western Province, Sri Lanka",
    )
    assert (jobs["222222"].company, jobs["222222"].location) == ("Contoso Health", "Remote")
    assert (jobs["333333"].title, jobs["333333"].company, jobs["333333"].location) == (
        "Data Platform Engineer",
        "Fabrikam",
        None,
    )
    assert jobs["111111"].url == "https://www.linkedin.com/jobs/view/111111"


def test_alert_jobs_become_listings_without_fetching_linkedin(project_root: Path) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert_cards.eml").read_bytes()

    class FixtureMailbox:
        def fetch_unseen(self, mailbox: str = "INBOX") -> list[bytes]:
            return [message, message]

    listings = EmailAlertSource(FixtureMailbox()).listings()

    assert len(listings) == 3
    remote = next(listing for listing in listings if listing.source_job_id == "222222")
    assert remote.remote is True
    assert remote.source_name == "linkedin_alert"
    assert "Backend Engineer (Remote) at Contoso Health" in remote.description


def test_alert_card_parser_ignores_other_senders(project_root: Path) -> None:
    message = (project_root / "tests/fixtures/linkedin_alert_cards.eml").read_bytes()

    assert parse_linkedin_alert_jobs(message.replace(b"linkedin.com>", b"evil.example>")) == []
