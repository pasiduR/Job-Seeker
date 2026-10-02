"""Run the queue worker: ``python -m app.queue [--once] [--poll-seconds 5]``."""

from __future__ import annotations

import argparse
import logging
import socket
from time import sleep

from psycopg import Connection

from app.config import RuntimeSettings, SecretSettings
from app.db.connection import open_pool
from app.http import HttpClient
from app.queue.pipeline_runner import PipelineRunner, PostgresPipelineStore
from app.queue.postgres import PostgresQueue
from app.queue.tasks import PipelineTasks, PostgresWorkerStore
from app.queue.worker import Worker
from app.sources.ats_api import AtsApiSource
from app.sources.dispatch import SourceDispatcher
from app.sources.finder import PublicSourceCatalog, SourceFinder
from app.sources.jobspy import JobSpySource
from app.sources.models import SourceRepository
from app.sources.rss import RemoteBoardSource
from app.sources.scraper import PostgresJobStore, ScraperService
from app.steps.base_cv import BaseCVService, PostgresBaseCVStore
from app.steps.latex import LatexCompiler


HTTP_TIMEOUT_SECONDS = 30.0
JOBSPY_TIMEOUT_SECONDS = 120.0


def build_tasks(connection: Connection, results_wanted: int) -> PipelineTasks:
    http = HttpClient(
        timeout_seconds=HTTP_TIMEOUT_SECONDS, source_minimum_intervals={}
    )
    pipeline_store = PostgresPipelineStore(connection)
    # Scorer, tailor, and career-page extraction need an LLM transport, which
    # is not configured yet (see TASKS.md); their queue items fail loudly
    # while scraping and source discovery still run.
    return PipelineTasks(
        store=PostgresWorkerStore(connection),
        finder=SourceFinder(SourceRepository(connection), [PublicSourceCatalog()]),
        dispatcher=SourceDispatcher(
            boards=RemoteBoardSource(http),
            ats=AtsApiSource(http),
            jobspy=JobSpySource(timeout_seconds=JOBSPY_TIMEOUT_SECONDS),
            jobspy_results_wanted=results_wanted,
        ),
        scraper=ScraperService(PostgresJobStore(connection)),
        base_cv=BaseCVService(
            store=PostgresBaseCVStore(connection), compiler=LatexCompiler()
        ),
        runner_factory=lambda steps: PipelineRunner(pipeline_store, steps),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process one item and exit")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    secrets = SecretSettings()
    if secrets.database_url is None:
        raise SystemExit("DATABASE_URL must be set in .env")
    pool = open_pool(secrets.database_url.get_secret_value())
    worker_id = f"{socket.gethostname()}-worker"

    while True:
        with pool.connection() as connection:
            settings = RuntimeSettings.model_validate(
                PostgresWorkerStore(connection).read_settings()
            )
            tasks = build_tasks(connection, settings.jobspy_results_wanted)
            worker = Worker(PostgresQueue(connection), worker_id, tasks.handlers())
            processed = worker.run_once()
        if args.once:
            return
        if not processed:
            sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
