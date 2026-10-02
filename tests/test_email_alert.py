from pathlib import Path

from app.sources.email_alert import links_from_alerts, parse_linkedin_alert


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
