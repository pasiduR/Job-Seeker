"""Read/write queries behind the dashboard pages."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import read_settings_table
from app.sources.models import Source, SourceCreate, SourceRepository, SourceUpdate
from app.steps.base_cv import BaseCVService, CVVersion, PostgresBaseCVStore
from app.triggers.manual import ManualTrigger
from app.triggers.review import ReviewDecisions


class SearchFilterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote: bool | None = None
    exclude_keywords: list[str] = Field(default_factory=list)

    @field_validator("roles", "locations", "exclude_keywords")
    @classmethod
    def strip_blank(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]


@dataclass(frozen=True)
class SearchFilterRow:
    id: int
    name: str
    roles: list[str]
    locations: list[str]
    remote: bool | None
    exclude_keywords: list[str]
    active: bool


@dataclass(frozen=True)
class JobRow:
    id: int
    title: str
    company: str
    url: str
    location: str | None
    status: str
    score: int | None
    created_at: datetime


@dataclass(frozen=True)
class ReviewItem:
    job_id: int
    title: str
    company: str
    url: str
    score: int | None
    answers: list[dict[str, Any]]
    screenshot_path: str | None
    cv_diff: str | None  # None when the base CV is used unchanged

    @property
    def flagged(self) -> list[dict[str, Any]]:
        return [
            answer
            for answer in self.answers
            if answer.get("flag") or answer.get("source") in {"drafted", "unknown"}
        ]


@dataclass(frozen=True)
class SkillRow:
    job_id: int
    job_title: str
    company: str
    skill: str
    estimated_days: int
    learning_plan: str


@dataclass(frozen=True)
class RunLogRow:
    run_id: str
    trigger: str
    step: str | None
    job_id: int | None
    status: str
    duration_ms: int | None
    error: str | None
    created_at: datetime


class DashboardStore(Protocol):
    def list_sources(self) -> list[Source]: ...

    def create_source(self, values: SourceCreate) -> tuple[Source, bool]: ...

    def set_source_active(self, source_id: int, active: bool) -> bool: ...

    def delete_source(self, source_id: int) -> bool: ...

    def list_filters(self) -> list[SearchFilterRow]: ...

    def create_filter(self, values: SearchFilterCreate) -> bool: ...

    def set_filter_active(self, filter_id: int, active: bool) -> bool: ...

    def delete_filter(self, filter_id: int) -> bool: ...

    def get_profile(self) -> dict[str, Any]: ...

    def save_profile(self, data: Mapping[str, Any]) -> None: ...

    def get_base(self) -> CVVersion | None: ...

    def list_jobs(
        self, *, status: str | None, min_score: int | None, limit: int
    ) -> list[JobRow]: ...

    def list_skills(self) -> list[SkillRow]: ...

    def list_review_items(self, *, limit: int) -> list[ReviewItem]: ...

    def get_screenshot_path(self, job_id: int) -> str | None: ...

    def list_run_logs(self, *, limit: int) -> list[RunLogRow]: ...

    def read_settings(self) -> dict[str, Any]: ...

    def save_settings(self, values: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class DashboardRepos:
    store: DashboardStore
    base_cv: BaseCVService
    trigger: ManualTrigger
    decisions: ReviewDecisions


class DashboardConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


def _json_object(value: object) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return dict(decoded) if isinstance(decoded, Mapping) else {}


class PostgresDashboardStore:
    def __init__(self, connection: DashboardConnection) -> None:
        self._connection = connection
        self._sources = SourceRepository(connection)
        self._base_cv = PostgresBaseCVStore(connection)

    def _rows(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        with self._connection.transaction():
            return list(self._connection.execute(query, params))

    def _changed(self, query: str, params: tuple[object, ...]) -> bool:
        return bool(self._rows(query, params))

    def list_sources(self) -> list[Source]:
        return self._sources.list()

    def create_source(self, values: SourceCreate) -> tuple[Source, bool]:
        return self._sources.create(values)

    def set_source_active(self, source_id: int, active: bool) -> bool:
        return self._sources.update(source_id, SourceUpdate(active=active)) is not None

    def delete_source(self, source_id: int) -> bool:
        return self._sources.delete(source_id)

    def list_filters(self) -> list[SearchFilterRow]:
        rows = self._rows(
            """
            SELECT id, name, roles, locations, remote, exclude_keywords, active
            FROM search_filters ORDER BY name, id
            """
        )
        return [
            SearchFilterRow(
                id=int(row[0]),
                name=str(row[1]),
                roles=list(row[2] or []),
                locations=list(row[3] or []),
                remote=row[4],
                exclude_keywords=list(row[5] or []),
                active=bool(row[6]),
            )
            for row in rows
        ]

    def create_filter(self, values: SearchFilterCreate) -> bool:
        return self._changed(
            """
            INSERT INTO search_filters (name, roles, locations, remote, exclude_keywords)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            (
                values.name,
                values.roles,
                values.locations,
                values.remote,
                values.exclude_keywords,
            ),
        )

    def set_filter_active(self, filter_id: int, active: bool) -> bool:
        return self._changed(
            """
            UPDATE search_filters SET active = %s, updated_at = now()
            WHERE id = %s RETURNING id
            """,
            (active, filter_id),
        )

    def delete_filter(self, filter_id: int) -> bool:
        return self._changed(
            "DELETE FROM search_filters WHERE id = %s RETURNING id", (filter_id,)
        )

    def get_profile(self) -> dict[str, Any]:
        rows = self._rows("SELECT data FROM profile WHERE id = 1")
        return _json_object(rows[0][0]) if rows else {}

    def save_profile(self, data: Mapping[str, Any]) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO profile (id, data) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                """,
                (json.dumps(dict(data)),),
            )

    def get_base(self) -> CVVersion | None:
        return self._base_cv.get_base()

    def list_jobs(
        self, *, status: str | None, min_score: int | None, limit: int
    ) -> list[JobRow]:
        conditions: list[str] = []
        params: list[object] = []
        if status:
            conditions.append("status = %s")
            params.append(status)
        if min_score is not None:
            conditions.append("score >= %s")
            params.append(min_score)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = self._rows(
            f"""
            SELECT id, title, company, url, location, status, score, created_at
            FROM jobs {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return [
            JobRow(
                id=int(row[0]),
                title=str(row[1]),
                company=str(row[2]),
                url=str(row[3]),
                location=row[4],
                status=str(row[5]),
                score=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    def list_review_items(self, *, limit: int) -> list[ReviewItem]:
        rows = self._rows(
            """
            SELECT j.id, j.title, j.company, j.url, j.score, f.answers,
                   f.screenshot_path, c.diff_from_base
            FROM jobs j
            JOIN form_fills f ON f.job_id = j.id AND f.outcome = 'filled'
            LEFT JOIN cv_versions c ON c.job_id = j.id
            WHERE j.status = 'filled'
            ORDER BY j.updated_at, j.id
            LIMIT %s
            """,
            (limit,),
        )
        return [
            ReviewItem(
                job_id=int(row[0]),
                title=str(row[1]),
                company=str(row[2]),
                url=str(row[3]),
                score=row[4],
                answers=list(json.loads(row[5]) if isinstance(row[5], str) else row[5]),
                screenshot_path=row[6],
                cv_diff=row[7],
            )
            for row in rows
        ]

    def get_screenshot_path(self, job_id: int) -> str | None:
        rows = self._rows(
            "SELECT screenshot_path FROM form_fills WHERE job_id = %s", (job_id,)
        )
        return rows[0][0] if rows else None

    def list_skills(self) -> list[SkillRow]:
        rows = self._rows(
            """
            SELECT s.job_id, j.title, j.company, s.skill, s.estimated_days,
                   s.learning_plan
            FROM skills_to_learn s JOIN jobs j ON j.id = s.job_id
            ORDER BY s.created_at DESC, s.id DESC
            """
        )
        return [
            SkillRow(
                job_id=int(row[0]),
                job_title=str(row[1]),
                company=str(row[2]),
                skill=str(row[3]),
                estimated_days=int(row[4]),
                learning_plan=str(row[5]),
            )
            for row in rows
        ]

    def list_run_logs(self, *, limit: int) -> list[RunLogRow]:
        rows = self._rows(
            """
            SELECT run_id, trigger, step, job_id, status, duration_ms, error,
                   created_at
            FROM run_logs ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (limit,),
        )
        return [
            RunLogRow(
                run_id=str(row[0]),
                trigger=str(row[1]),
                step=row[2],
                job_id=row[3],
                status=str(row[4]),
                duration_ms=row[5],
                error=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    def read_settings(self) -> dict[str, Any]:
        with self._connection.transaction():
            return read_settings_table(self._connection)

    def save_settings(self, values: Mapping[str, Any]) -> None:
        with self._connection.transaction():
            for key, value in values.items():
                self._connection.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                    """,
                    (key, json.dumps(value)),
                )
