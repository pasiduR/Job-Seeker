"""Run the queue worker: ``python -m app.queue [--once] [--poll-seconds 5]``."""

from __future__ import annotations

import argparse
import logging
import socket
from dataclasses import dataclass
from time import sleep

from psycopg import Connection

from app.browser.session import BrowserOptions, open_form_page
from app.config import RuntimeSettings, SecretSettings
from app.db.connection import open_pool
from app.http import HttpClient
from app.llm.client import AnthropicTransport, LLMCallLogger, LLMClient, LLMNotConfigured
from app.queue.pipeline_runner import PipelineRunner, PostgresPipelineStore
from app.queue.postgres import PostgresQueue
from app.queue.tasks import PipelineTasks, PostgresWorkerStore
from app.queue.worker import Worker
from app.sources.ats_api import AtsApiSource
from app.sources.career_page import CareerPageSource, PlaywrightPageFetcher
from app.sources.dispatch import SourceDispatcher
from app.sources.email_alert import EmailAlertSource, ImapAlertFetcher
from app.sources.finder import PublicSourceCatalog, SourceFinder
from app.sources.jobspy import JobSpySource
from app.sources.models import SourceRepository
from app.sources.rss import RemoteBoardSource
from app.sources.scraper import PostgresJobStore, ScraperService
from app.sources.rate_limit import PostgresRateLimiter
from app.sources.models import SourceType
from app.steps.base_cv import BaseCVService, PostgresBaseCVStore
from app.steps.fill import FillService, PostgresFormFillStore, PostgresNotificationQueue
from app.steps.form_filler import FillLimits, FormFiller
from app.steps.form_mapper import FormMapper
from app.steps.latex import LatexCompiler
from app.steps.submit import PostgresSubmitStore, SubmitService
from app.steps.scorer import PostgresScoreStore, Scorer
from app.steps.tailor import PostgresTailoredCVStore, Tailor
from app.triggers.watcher import Watcher


HTTP_TIMEOUT_SECONDS = 30.0
JOBSPY_TIMEOUT_SECONDS = 120.0
IMAP_TIMEOUT_SECONDS = 30.0
CAREER_PAGE_TIMEOUT_SECONDS = 30.0
APPLICATION_BROWSER_PROFILE = "applications"


@dataclass(frozen=True)
class LLMSteps:
    scorer: Scorer
    tailor: Tailor
    career_pages: CareerPageSource
    filler: FillService


def email_alerts(secrets: SecretSettings) -> EmailAlertSource | None:
    if not (secrets.imap_host and secrets.imap_username and secrets.imap_password):
        return None
    return EmailAlertSource(
        ImapAlertFetcher(
            host=secrets.imap_host,
            username=secrets.imap_username,
            password=secrets.imap_password.get_secret_value(),
            timeout_seconds=IMAP_TIMEOUT_SECONDS,
        )
    )


def llm_steps(
    connection: Connection, secrets: SecretSettings, settings: RuntimeSettings
) -> LLMSteps | None:
    """LLM-backed steps, or None when ANTHROPIC_API_KEY is missing.

    Without them, score/tailor queue items fail loudly (jobs untouched) and
    career-page sources report an error, while scraping still runs.
    """

    try:
        transport = AnthropicTransport.from_config(secrets, settings)
    except LLMNotConfigured:
        return None
    llm: LLMClient = LLMClient(transport, LLMCallLogger(connection))
    return LLMSteps(
        scorer=Scorer(
            llm=llm, store=PostgresScoreStore(connection), model=settings.llm_model
        ),
        tailor=Tailor(
            llm=llm,
            compiler=LatexCompiler(),
            store=PostgresTailoredCVStore(connection),
            model=settings.llm_model,
        ),
        career_pages=CareerPageSource(
            fetcher=PlaywrightPageFetcher(timeout_seconds=CAREER_PAGE_TIMEOUT_SECONDS),
            llm=llm,
            model=settings.llm_model,
        ),
        filler=FillService(
            filler=FormFiller(
                llm=llm,
                mapper=FormMapper(llm=llm, model=settings.llm_model),
                model=settings.llm_model,
                limits=FillLimits.from_settings(settings),
            ),
            store=PostgresFormFillStore(connection),
            open_form=lambda url: open_form_page(browser_options(settings), url),
            notifications=PostgresNotificationQueue(connection),
        ),
    )


def browser_options(settings: RuntimeSettings) -> BrowserOptions:
    return BrowserOptions(
        profile=APPLICATION_BROWSER_PROFILE,
        headless=settings.browser_headless,
        timeout_seconds=settings.browser_timeout_seconds,
    )


def build_tasks(
    connection: Connection, settings: RuntimeSettings, secrets: SecretSettings
) -> PipelineTasks:
    limiter = PostgresRateLimiter(connection, settings.source_request_intervals_seconds)
    http = HttpClient(timeout_seconds=HTTP_TIMEOUT_SECONDS, source_minimum_intervals={}, rate_limiter=limiter)
    pipeline_store = PostgresPipelineStore(connection)
    steps = llm_steps(connection, secrets, settings)
    dispatcher = SourceDispatcher(
        boards=RemoteBoardSource(http), ats=AtsApiSource(http),
        jobspy=JobSpySource(timeout_seconds=JOBSPY_TIMEOUT_SECONDS, rate_limiter=limiter),
        career_pages=steps.career_pages if steps else None,
        email_alerts=email_alerts(secrets), jobspy_results_wanted=settings.jobspy_results_wanted,
        before_fetch=lambda source: limiter.wait(f"{source.type.value}:{source.id}")
            if source.type in {SourceType.CAREER_PAGE, SourceType.EMAIL_ALERT} else None,
    )
    return PipelineTasks(
        store=PostgresWorkerStore(connection),
        finder=SourceFinder(SourceRepository(connection), [PublicSourceCatalog()]),
        dispatcher=dispatcher,
        watcher=Watcher(connection, dispatcher, settings),
        scraper=ScraperService(PostgresJobStore(connection)),
        base_cv=BaseCVService(
            store=PostgresBaseCVStore(connection), compiler=LatexCompiler()
        ),
        runner_factory=lambda job_steps: PipelineRunner(pipeline_store, job_steps),
        submitter=SubmitService(
            store=PostgresSubmitStore(connection, timezone_name=settings.automation_timezone),
            open_form=lambda url: open_form_page(browser_options(settings), url),
        ),
        scorer=steps.scorer if steps else None,
        tailor=steps.tailor if steps else None,
        filler=steps.filler if steps else None,
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
            tasks = build_tasks(connection, settings, secrets)
            worker = Worker(PostgresQueue(connection), worker_id, tasks.handlers())
            processed = worker.run_once()
        if args.once:
            return
        if not processed:
            sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
