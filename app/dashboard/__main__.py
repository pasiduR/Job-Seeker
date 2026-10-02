"""Run the dashboard: ``python -m app.dashboard [--host 127.0.0.1] [--port 8000]``."""

from __future__ import annotations

import argparse

import uvicorn

from app.config import SecretSettings
from app.dashboard.app import DashboardCredentials, create_app
from app.db.connection import open_pool, pooled_connection


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
        repo_factory=lambda: pooled_connection(pool),
        credentials=DashboardCredentials(
            username=secrets.dashboard_username,
            password=secrets.dashboard_password,
        ),
    )
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()
