from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from app.db.migrate import apply_migrations, discover_migrations


class FakeMigrationConnection:
    def __init__(self) -> None:
        self.applied: dict[str, str] = {}
        self.executed: list[str] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]:
        self.executed.append(query)
        if query == "SELECT version, checksum FROM schema_migrations":
            return list(self.applied.items())
        if query.startswith("INSERT INTO schema_migrations"):
            assert params is not None
            self.applied[str(params[0])] = str(params[1])
        return []


def test_initial_migration_creates_every_required_table(project_root: Path) -> None:
    migration = discover_migrations()[0]
    required_tables = (
        project_root / "tests/fixtures/required_tables.txt"
    ).read_text(encoding="utf-8").splitlines()
    created_tables = set(
        re.findall(r"CREATE TABLE\s+(\w+)", migration.sql, flags=re.IGNORECASE)
    )

    assert set(required_tables) <= created_tables


def test_initial_migration_encodes_critical_uniqueness() -> None:
    sql = discover_migrations()[0].sql

    assert re.search(
        r"job_id\s+BIGINT\s+NOT NULL\s+UNIQUE\s+REFERENCES\s+jobs",
        sql,
        flags=re.IGNORECASE,
    )
    assert "CREATE UNIQUE INDEX jobs_url_unique" in sql
    assert "CREATE UNIQUE INDEX jobs_company_title_unique" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql


def test_migration_runner_is_idempotent() -> None:
    connection = FakeMigrationConnection()

    assert apply_migrations(connection) == [migration.version for migration in discover_migrations()]
    executed_after_first_run = len(connection.executed)
    assert apply_migrations(connection) == []
    second_run_queries = connection.executed[executed_after_first_run:]

    assert all("CREATE TABLE sources" not in query for query in second_run_queries)
