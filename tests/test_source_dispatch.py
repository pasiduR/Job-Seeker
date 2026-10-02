from datetime import datetime
from typing import Any

import pytest

from app.sources.dispatch import (
    SourceDispatcher,
    UnsupportedSource,
    ats_target,
    depends_on_filter,
)
from app.sources.models import Source, SourceCreate, SourceType
from app.sources.scraper import SearchFilter


NOW = datetime(2026, 10, 1)


def source(type_: SourceType, url: str, **config: Any) -> Source:
    values = SourceCreate(name="Acme", type=type_, url=url, config=config)
    return Source(id=1, created_at=NOW, updated_at=NOW, **values.model_dump())


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(**kwargs: Any) -> list[Any]:
            self.calls.append((name, kwargs))
            return []

        return record


def dispatcher(**overrides: Any) -> tuple[SourceDispatcher, Recorder, Recorder, Recorder]:
    boards, ats, jobspy = Recorder(), Recorder(), Recorder()
    values = {"boards": boards, "ats": ats, "jobspy": jobspy, "jobspy_results_wanted": 15}
    return SourceDispatcher(**{**values, **overrides}), boards, ats, jobspy


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/acme/jobs/1", ("greenhouse", "acme")),
        ("https://jobs.lever.co/acme", ("lever", "acme")),
        ("https://jobs.ashbyhq.com/acme", ("ashby", "acme")),
    ],
)
def test_ats_board_is_inferred_from_url(url: str, expected: tuple[str, str]) -> None:
    assert ats_target(source(SourceType.ATS_BOARD, url)) == expected


def test_ats_config_overrides_url_and_unknown_hosts_are_rejected() -> None:
    configured = source(SourceType.ATS_BOARD, "https://acme.example/careers", provider="lever", board="acme-inc")
    assert ats_target(configured) == ("lever", "acme-inc")
    with pytest.raises(UnsupportedSource):
        ats_target(source(SourceType.ATS_BOARD, "https://acme.example/careers"))


def test_ats_sources_call_the_matching_api_with_company_name() -> None:
    subject, _, ats, _ = dispatcher()
    subject.fetch(source(SourceType.ATS_BOARD, "https://jobs.ashbyhq.com/acme"), SearchFilter())

    assert ats.calls == [("ashby", {"board_name": "acme", "company": "Acme"})]


def test_jobspy_searches_each_role_and_location_at_low_volume() -> None:
    subject, _, _, jobspy = dispatcher()
    board = source(SourceType.JOB_BOARD, "https://www.indeed.com", adapter="jobspy", site_name="indeed")

    subject.fetch(board, SearchFilter(roles=("Python", "Backend"), locations=("Colombo",)))

    assert depends_on_filter(board) is True
    assert [(kwargs["search_term"], kwargs["location"], kwargs["results_wanted"]) for _, kwargs in jobspy.calls] == [
        ("Python", "Colombo", 15),
        ("Backend", "Colombo", 15),
    ]
    with pytest.raises(UnsupportedSource, match="role"):
        subject.fetch(board, SearchFilter())


def test_remote_boards_and_rss_use_board_adapters() -> None:
    subject, boards, _, _ = dispatcher()
    remotive = source(SourceType.JOB_BOARD, "https://remotive.com", adapter="remotive")

    subject.fetch(remotive, SearchFilter())
    subject.fetch(source(SourceType.RSS, "https://acme.example/jobs.xml"), SearchFilter())

    assert depends_on_filter(remotive) is False
    assert boards.calls == [
        ("remotive", {}),
        ("feed", {"url": "https://acme.example/jobs.xml", "default_company": "Acme"}),
    ]


def test_sources_without_an_adapter_are_reported() -> None:
    subject, _, _, _ = dispatcher()

    with pytest.raises(UnsupportedSource, match="IMAP"):
        subject.fetch(source(SourceType.EMAIL_ALERT, "imap://inbox"), SearchFilter())
    with pytest.raises(UnsupportedSource, match="LLM"):
        subject.fetch(source(SourceType.CAREER_PAGE, "https://acme.example/careers"), SearchFilter())


def test_email_alert_sources_read_the_mailbox_once() -> None:
    alerts = Recorder()
    subject, _, _, _ = dispatcher(email_alerts=alerts)
    alert_source = source(SourceType.EMAIL_ALERT, "imap://inbox")

    subject.fetch(alert_source, SearchFilter())

    assert depends_on_filter(alert_source) is False
    assert alerts.calls == [("listings", {})]
