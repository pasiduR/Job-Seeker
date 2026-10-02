import json
from datetime import datetime, timezone
from pathlib import Path

from app.sources.finder import (
    DiscoveryCriteria,
    PublicSourceCatalog,
    SourceFinder,
)
from app.sources.models import Source, SourceCreate, SourceType


class FixtureDiscovery:
    def __init__(self, candidates: list[SourceCreate]) -> None:
        self.candidates = candidates

    def discover(
        self, source_type: SourceType, criteria: DiscoveryCriteria
    ) -> list[SourceCreate]:
        return [value for value in self.candidates if value.type == source_type]


class FixtureSourceRepository:
    def __init__(self) -> None:
        self.values: dict[tuple[SourceType, str], Source] = {}

    def create(self, values: SourceCreate) -> tuple[Source, bool]:
        key = (values.type, values.url)
        existing = self.values.get(key)
        if existing is not None:
            return existing, False
        now = datetime.now(timezone.utc)
        source = Source(
            id=len(self.values) + 1,
            created_at=now,
            updated_at=now,
            **values.model_dump(),
        )
        self.values[key] = source
        return source, True


def test_source_finder_saves_catalog_and_discovered_career_pages(
    project_root: Path,
) -> None:
    candidates = [
        SourceCreate.model_validate(value)
        for value in json.loads(
            (project_root / "tests/fixtures/source_candidates.json").read_text(
                encoding="utf-8"
            )
        )
    ]
    repository = FixtureSourceRepository()
    finder = SourceFinder(
        repository,
        (PublicSourceCatalog(), FixtureDiscovery(candidates)),
    )

    result = finder.run(
        configured_types=(SourceType.JOB_BOARD, SourceType.CAREER_PAGE),
        criteria=DiscoveryCriteria(roles=("Python Engineer",), remote=True),
    )

    assert result.discovered == 6
    assert result.created == 6
    assert result.existing == 0
    assert len(repository.values) == 6

    second_result = finder.run(
        configured_types=(SourceType.JOB_BOARD, SourceType.CAREER_PAGE),
        criteria=DiscoveryCriteria(roles=("Python Engineer",), remote=True),
    )
    assert second_result.created == 0
    assert second_result.existing == 6
