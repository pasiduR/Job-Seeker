"""Run the dashboard: ``python -m app.dashboard [--host 127.0.0.1] [--port 8000]``."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from psycopg_pool import ConnectionPool

from app.config import SecretSettings
from app.dashboard.app import DashboardCredentials, create_app
from app.dashboard.pages import router as pages_router
from app.dashboard.repository import DashboardRepos, PostgresDashboardStore
from app.db.connection import open_pool
from app.queue.postgres import PostgresQueue
from app.steps.base_cv import BaseCVService, PostgresBaseCVStore
from app.steps.latex import LatexCompiler
from app.triggers.manual import ManualTrigger


def repo_factory(pool: ConnectionPool):  # type: ignore[no-untyped-def]
    compiler = LatexCompiler()

    @contextmanager
    def open_repos() -> Iterator[DashboardRepos]:
        with pool.connection() as connection:
            yield DashboardRepos(
                store=PostgresDashboardStore(connection),
                base_cv=BaseCVService(
                    store=PostgresBaseCVStore(connection), compiler=compiler
                ),
                trigger=ManualTrigger(PostgresQueue(connection)),
            )

    return open_repos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    if not secrets.dashboard_username or secrets.dashboard_password is None:
        raise SystemExit("DASHBOARD_USERNAME and DASHBOARD_PASSWORD must be set in .env")

    pool = open_pool(secrets.database_url.get_secret_value())
    app = create_app(
        repo_factory=repo_factory(pool),
        credentials=DashboardCredentials(
            username=secrets.dashboard_username,
            password=secrets.dashboard_password,
        ),
        routers=(pages_router,),
    )
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()
