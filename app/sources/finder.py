"""Deterministic source discovery and persistence service."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.sources.models import Source, SourceCreate, SourceType


class DiscoveryCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[str, ...] = Field(default_factory=tuple)
    locations: tuple[str, ...] = Field(default_factory=tuple)
    remote: bool | None = None
    countries: tuple[str, ...] = Field(default_factory=tuple)


class SourceDiscovery(Protocol):
    def discover(
        self, source_type: SourceType, criteria: DiscoveryCriteria
    ) -> Iterable[SourceCreate]: ...


class SourceWriter(Protocol):
    def create(self, values: SourceCreate) -> tuple[Source, bool]: ...


@dataclass(frozen=True)
class SourceFinderResult:
    discovered: int
    created: int
    existing: int


class PublicSourceCatalog:
    """Known supported public platforms; no probing or network calls."""

    _SOURCES = (
        SourceCreate(
            name="Indeed",
            type=SourceType.JOB_BOARD,
            url="https://www.indeed.com",
            config={"adapter": "jobspy", "site_name": "indeed"},
        ),
        SourceCreate(
            name="Glassdoor",
            type=SourceType.JOB_BOARD,
            url="https://www.glassdoor.com/Job",
            config={"adapter": "jobspy", "site_name": "glassdoor"},
        ),
        SourceCreate(
            name="Remotive",
            type=SourceType.JOB_BOARD,
            url="https://remotive.com/remote-jobs",
            config={"adapter": "remotive"},
        ),
        SourceCreate(
            name="RemoteOK",
            type=SourceType.JOB_BOARD,
            url="https://remoteok.com",
            config={"adapter": "remoteok"},
        ),
        SourceCreate(
            name="Arbeitnow",
            type=SourceType.JOB_BOARD,
            url="https://www.arbeitnow.com",
            config={"adapter": "arbeitnow"},
        ),
    )

    def discover(
        self, source_type: SourceType, criteria: DiscoveryCriteria
    ) -> Iterable[SourceCreate]:
        return (source for source in self._SOURCES if source.type == source_type)


class SourceFinder:
    def __init__(
        self,
        repository: SourceWriter,
        discoveries: Sequence[SourceDiscovery],
    ) -> None:
        self._repository = repository
        self._discoveries = discoveries

    def run(
        self,
        *,
        configured_types: Iterable[SourceType],
        criteria: DiscoveryCriteria,
    ) -> SourceFinderResult:
        discovered = 0
        created = 0
        existing = 0
        candidates_seen: set[tuple[SourceType, str]] = set()

        for source_type in dict.fromkeys(configured_types):
            for discovery in self._discoveries:
                for candidate in discovery.discover(source_type, criteria):
                    if candidate.type != source_type:
                        raise ValueError(
                            "source discovery returned a candidate of the wrong type"
                        )
                    key = (candidate.type, candidate.url)
                    if key in candidates_seen:
                        continue
                    candidates_seen.add(key)
                    discovered += 1
                    _, was_created = self._repository.create(candidate)
                    if was_created:
                        created += 1
                    else:
                        existing += 1

        return SourceFinderResult(
            discovered=discovered,
            created=created,
            existing=existing,
        )
