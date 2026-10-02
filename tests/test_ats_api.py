import json
from pathlib import Path

from app.http import HttpResponse
from app.sources.ats_api import AtsApiSource


class FixtureHttpClient:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        provider = source.partition(":")[0]
        self.calls.append((source, url))
        return HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(self.payloads[provider]).encode(),
        )


def test_ats_public_apis_normalize_fixture_payloads(project_root: Path) -> None:
    payloads = json.loads(
        (project_root / "tests/fixtures/ats_responses.json").read_text(
            encoding="utf-8"
        )
    )
    http = FixtureHttpClient(payloads)
    source = AtsApiSource(http)

    greenhouse = source.greenhouse(board_token="example", company="Example Co")
    lever = source.lever(site="example", company="Example Co")
    ashby = source.ashby(board_name="example", company="Example Co")

    assert greenhouse[0].description == "Build & operate\nPython\nservices."
    assert greenhouse[0].remote is True
    assert lever[0].source_job_id == "lever-202"
    assert lever[0].remote is False
    assert ashby[0].source_job_id == "ashby-303"
    assert ashby[0].remote is True
    assert all(listing.posted_at is not None for listing in (greenhouse + lever + ashby))
    assert [call[0] for call in http.calls] == [
        "greenhouse:example",
        "lever:example",
        "ashby:example",
    ]
