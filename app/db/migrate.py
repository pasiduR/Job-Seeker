"""Minimal transactional SQL migration runner for PostgreSQL."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_MIGRATION_LOCK_ID = 1_246_590_173


class MigrationConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str
    checksum: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    seen_versions: set[str] = set()
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = path.name.split("_", maxsplit=1)[0]
        if version in seen_versions:
            raise ValueError(f"Duplicate migration version: {version}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(Migration(version=version, sql=sql, checksum=checksum))
    return migrations


def apply_migrations(
    connection: MigrationConnection, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations once, under a transaction-scoped advisory lock."""

    migrations = discover_migrations(directory)
    applied_now: list[str] = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {
            str(version): str(checksum)
            for version, checksum in connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            )
        }

        for migration in migrations:
            existing_checksum = applied.get(migration.version)
            if existing_checksum is not None:
                if existing_checksum != migration.checksum:
                    raise RuntimeError(
                        f"Applied migration {migration.version} has changed"
                    )
                continue

            connection.execute(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
            applied_now.append(migration.version)

    return applied_now
