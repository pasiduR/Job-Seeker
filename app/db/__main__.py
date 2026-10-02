"""Apply pending database migrations: ``python -m app.db``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config import SecretSettings
from app.db.migrate import apply_migrations


def _connect(database_url: str) -> Any:
    import psycopg

    # autocommit: apply_migrations wraps everything in one transaction itself.
    return psycopg.connect(database_url, autocommit=True)


def main(connect: Callable[[str], Any] = _connect) -> list[str]:
    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    with connect(secrets.database_url.get_secret_value()) as connection:
        applied = apply_migrations(connection)
    print(f"Applied migrations: {', '.join(applied)}" if applied else "Database is up to date")
    return applied


if __name__ == "__main__":
    main()
