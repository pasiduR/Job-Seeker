"""Source models, URL normalization, and PostgreSQL CRUD operations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceType(StrEnum):
    JOB_BOARD = "job_board"
    ATS_BOARD = "ats_board"
    CAREER_PAGE = "career_page"
    RSS = "rss"
    EMAIL_ALERT = "email_alert"


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: SourceType
    url: str
    config: dict[str, object] = Field(default_factory=dict)
    active: bool = True

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return canonicalize_source_url(value)

    @field_validator("config")
    @classmethod
    def keep_secrets_out_of_database(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        secret_key = _find_secret_key(value)
        if secret_key is not None:
            raise ValueError(f"source config cannot contain secret field {secret_key!r}")
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    type: SourceType | None = None
    url: str | None = None
    config: dict[str, object] | None = None
    active: bool | None = None

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        return canonicalize_source_url(value) if value is not None else None

    @field_validator("config")
    @classmethod
    def keep_secrets_out_of_database(
        cls, value: dict[str, object] | None
    ) -> dict[str, object] | None:
        if value is not None:
            secret_key = _find_secret_key(value)
            if secret_key is not None:
                raise ValueError(
                    f"source config cannot contain secret field {secret_key!r}"
                )
        return value


class Source(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    type: SourceType
    url: str
    config: dict[str, object]
    active: bool
    created_at: datetime
    updated_at: datetime


class SourceConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class SourceRepository:
    _RETURNING = (
        "id, name, type, url, config, active, created_at, updated_at"
    )

    def __init__(self, connection: SourceConnection) -> None:
        self._connection = connection

    def create(self, values: SourceCreate) -> tuple[Source, bool]:
        with self._connection.transaction():
            rows = self._connection.execute(
                f"""
                INSERT INTO sources (name, type, url, config, active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (type, url) DO NOTHING
                RETURNING {self._RETURNING}
                """,
                (
                    values.name,
                    values.type.value,
                    values.url,
                    json.dumps(values.config),
                    values.active,
                ),
            )
            row = next(iter(rows), None)
            if row is not None:
                return _source_from_row(row), True

            existing_rows = self._connection.execute(
                f"SELECT {self._RETURNING} FROM sources WHERE type = %s AND url = %s",
                (values.type.value, values.url),
            )
            existing = next(iter(existing_rows), None)
            if existing is None:
                raise RuntimeError("source insert conflicted but existing row was not found")
            return _source_from_row(existing), False

    def get(self, source_id: int) -> Source | None:
        with self._connection.transaction():
            rows = self._connection.execute(
                f"SELECT {self._RETURNING} FROM sources WHERE id = %s",
                (source_id,),
            )
            row = next(iter(rows), None)
        return _source_from_row(row) if row is not None else None

    def list(self, *, active_only: bool = False) -> list[Source]:
        query = f"SELECT {self._RETURNING} FROM sources"
        if active_only:
            query += " WHERE active = TRUE"
        query += " ORDER BY name, id"
        with self._connection.transaction():
            rows = list(self._connection.execute(query))
        return [_source_from_row(row) for row in rows]

    def update(self, source_id: int, values: SourceUpdate) -> Source | None:
        changes = values.model_dump(exclude_none=True)
        if not changes:
            return self.get(source_id)

        assignments: list[str] = []
        parameters: list[object] = []
        for field_name in ("name", "type", "url", "config", "active"):
            if field_name not in changes:
                continue
            assignments.append(f"{field_name} = %s")
            value = changes[field_name]
            if isinstance(value, SourceType):
                value = value.value
            elif field_name == "config":
                value = json.dumps(value)
            parameters.append(value)
        assignments.append("updated_at = now()")
        parameters.append(source_id)

        with self._connection.transaction():
            rows = self._connection.execute(
                f"""
                UPDATE sources SET {', '.join(assignments)}
                WHERE id = %s
                RETURNING {self._RETURNING}
                """,
                tuple(parameters),
            )
            row = next(iter(rows), None)
        return _source_from_row(row) if row is not None else None

    def delete(self, source_id: int) -> bool:
        with self._connection.transaction():
            rows = self._connection.execute(
                "DELETE FROM sources WHERE id = %s RETURNING id", (source_id,)
            )
            return next(iter(rows), None) is not None


def canonicalize_source_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError("source URL must be absolute")
    if parts.username is not None or parts.password is not None:
        raise ValueError("source URL must not contain credentials")

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parts.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, hostname, path, query, ""))


def _source_from_row(row: tuple[Any, ...]) -> Source:
    raw_config = row[4]
    config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    return Source(
        id=row[0],
        name=row[1],
        type=row[2],
        url=row[3],
        config=config,
        active=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _find_secret_key(values: object) -> str | None:
    secret_fragments = ("secret", "password", "token", "api_key", "cookie")
    if isinstance(values, Mapping):
        for key, value in values.items():
            normalized = key.lower().replace("-", "_")
            if any(fragment in normalized for fragment in secret_fragments):
                return key
            nested = _find_secret_key(value)
            if nested is not None:
                return nested
    elif isinstance(values, list):
        for value in values:
            nested = _find_secret_key(value)
            if nested is not None:
                return nested
    return None
