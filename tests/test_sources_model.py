import json
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.sources.models import (
    SourceCreate,
    SourceRepository,
    canonicalize_source_url,
)


class SourceConnectionFixture:
    def __init__(self, responses: Iterable[Iterable[tuple[Any, ...]]]) -> None:
        self._responses = iter(responses)
        self.queries: list[str] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]:
        self.queries.append(query)
        return next(self._responses)


def _source_row(project_root: Path) -> tuple[object, ...]:
    fixture = json.loads(
        (project_root / "tests/fixtures/source.json").read_text(encoding="utf-8")
    )
    return (
        fixture["id"],
        fixture["name"],
        fixture["type"],
        fixture["url"],
        fixture["config"],
        fixture["active"],
        datetime.fromisoformat(fixture["created_at"]),
        datetime.fromisoformat(fixture["updated_at"]),
    )


def test_source_urls_are_canonicalized_for_dedupe() -> None:
    assert canonicalize_source_url(
        "HTTPS://Example.COM:443/careers/?b=2&a=1#openings"
    ) == "https://example.com/careers?a=1&b=2"


def test_source_config_rejects_secrets() -> None:
    with pytest.raises(ValidationError):
        SourceCreate(
            name="Unsafe",
            type="rss",
            url="https://example.com/feed",
            config={"api_key": "must-not-be-stored"},
        )


def test_repository_returns_existing_source_when_insert_is_duplicate(
    project_root: Path,
) -> None:
    row = _source_row(project_root)
    connection = SourceConnectionFixture([[], [row]])
    repository = SourceRepository(connection)
    source_values = SourceCreate(
        name="Example Careers",
        type="career_page",
        url="https://example.com/careers/",
        config={"polling_interval_minutes": 30},
    )

    source, created = repository.create(source_values)

    assert created is False
    assert source.id == 7
    assert source.url == "https://example.com/careers"
    assert "ON CONFLICT (type, url) DO NOTHING" in connection.queries[0]
