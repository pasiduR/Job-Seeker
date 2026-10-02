import json
from pathlib import Path

from app.http import HttpResponse
from app.sources.rss import RemoteBoardSource, parse_feed


class RemoteFixtureHttp:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(self.payloads[source]).encode(),
        )


def test_remote_board_apis_normalize_fixture_payloads(project_root: Path) -> None:
    payloads = json.loads(
        (project_root / "tests/fixtures/remote_board_responses.json").read_text(
            encoding="utf-8"
        )
    )
    source = RemoteBoardSource(RemoteFixtureHttp(payloads))

    remotive = source.remotive()
    remote_ok = source.remote_ok()
    arbeitnow = source.arbeitnow()

    assert remotive[0].description == "Build APIs."
    assert remotive[0].remote is True
    assert remote_ok[0].source_job_id == "12"
    assert remote_ok[0].remote is True
    assert arbeitnow[0].source_job_id == "backend-13"
    assert arbeitnow[0].remote is True


def test_rss_parser_normalizes_fixture_feed(project_root: Path) -> None:
    content = (project_root / "tests/fixtures/jobs_feed.xml").read_bytes()

    listings = parse_feed(content, default_company="Fallback Co")

    assert len(listings) == 1
    assert listings[0].title == "Site Reliability Engineer"
    assert listings[0].company == "Feed Co"
    assert listings[0].description == "Keep systems reliable."
    assert listings[0].source_job_id == "rss-14"
    assert listings[0].posted_at is not None
