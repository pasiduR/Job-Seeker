"""Queue task handlers: deterministic control flow around the pipeline steps."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from app.config import RuntimeSettings, read_settings_table
from app.queue.pipeline_runner import (
    PipelineRunner,
    PipelineStep,
    RunResult,
    StepOutcome,
)
from app.queue.postgres import QueueItem
from app.queue.state_machine import JobStatus
from app.sources.dispatch import SourceDispatcher, depends_on_filter
from app.sources.finder import DiscoveryCriteria, SourceFinder
from app.sources.models import Source, SourceRepository, SourceType
from app.sources.scraper import ScraperService, SearchFilter
from app.sources.types import JobListing
from app.steps.base_cv import BaseCVService, CVVersion
from app.steps.latex import latex_to_text
from app.steps.scorer import Scorer
from app.steps.tailor import Tailor, TailorSettings
from app.triggers.manual import RUN_JOB_TASK, RUN_PIPELINE_TASK


JOB_STEPS = ("score", "tailor")
_STEP_STATUSES = {"score": JobStatus.FOUND, "tailor": JobStatus.SCORED}


class PipelineBusy(RuntimeError):
    """Another run holds the pipeline lock; the queue retries later."""


class PipelineNotReady(RuntimeError):
    """A prerequisite (LLM transport, base CV) is missing; jobs stay untouched."""


@dataclass(frozen=True)
class WorkerJob:
    id: int
    description: str
    score: int | None


class WorkerStore(Protocol):
    def active_sources(self) -> list[Source]: ...

    def active_filters(self) -> list[SearchFilter]: ...

    def job_ids_with_status(self, statuses: Collection[JobStatus]) -> list[int]: ...

    def get_job(self, job_id: int) -> WorkerJob: ...

    def read_settings(self) -> dict[str, Any]: ...

    def write_log(
        self,
        *,
        run_id: UUID,
        trigger: str,
        step: str,
        job_id: int | None,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None: ...


class WorkerConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


class PostgresWorkerStore:
    def __init__(self, connection: WorkerConnection) -> None:
        self._connection = connection

    def _rows(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> list[tuple[Any, ...]]:
        with self._connection.transaction():
            return list(self._connection.execute(query, params))

    def active_sources(self) -> list[Source]:
        return SourceRepository(self._connection).list(active_only=True)

    def active_filters(self) -> list[SearchFilter]:
        rows = self._rows(
            """
            SELECT roles, locations, remote, exclude_keywords
            FROM search_filters WHERE active ORDER BY id
            """
        )
        return [
            SearchFilter(
                roles=tuple(row[0] or ()),
                locations=tuple(row[1] or ()),
                remote=row[2],
                exclude_keywords=tuple(row[3] or ()),
            )
            for row in rows
        ]

    def job_ids_with_status(self, statuses: Collection[JobStatus]) -> list[int]:
        rows = self._rows(
            "SELECT id FROM jobs WHERE status = ANY(%s) ORDER BY id",
            ([status.value for status in statuses],),
        )
        return [int(row[0]) for row in rows]

    def get_job(self, job_id: int) -> WorkerJob:
        rows = self._rows(
            "SELECT id, description, score FROM jobs WHERE id = %s", (job_id,)
        )
        if not rows:
            raise LookupError(f"Job {job_id} does not exist")
        row = rows[0]
        return WorkerJob(id=int(row[0]), description=str(row[1]), score=row[2])

    def read_settings(self) -> dict[str, Any]:
        with self._connection.transaction():
            return read_settings_table(self._connection)  # type: ignore[arg-type]

    def write_log(
        self,
        *,
        run_id: UUID,
        trigger: str,
        step: str,
        job_id: int | None,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO run_logs (
                    run_id, trigger, step, job_id, status, duration_ms, error
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, trigger, step, job_id, status, duration_ms, error),
            )


RunnerFactory = Callable[[Sequence[PipelineStep]], PipelineRunner]


class PipelineTasks:
    """Handlers for ``run_pipeline`` and ``run_job`` queue items.

    ``scorer`` and ``tailor`` are None until an LLM transport is configured;
    LLM steps then fail the queue item, never the jobs.
    """

    def __init__(
        self,
        *,
        store: WorkerStore,
        finder: SourceFinder,
        dispatcher: SourceDispatcher,
        scraper: ScraperService,
        base_cv: BaseCVService,
        runner_factory: RunnerFactory,
        scorer: Scorer | None = None,
        tailor: Tailor | None = None,
    ) -> None:
        self._store = store
        self._finder = finder
        self._dispatcher = dispatcher
        self._scraper = scraper
        self._base_cv = base_cv
        self._runner_factory = runner_factory
        self._scorer = scorer
        self._tailor = tailor

    def handlers(self) -> dict[str, Callable[[QueueItem], None]]:
        return {RUN_PIPELINE_TASK: self.run_pipeline, RUN_JOB_TASK: self.run_job}

    def run_pipeline(self, item: QueueItem) -> None:
        trigger = str(item.payload.get("trigger", "manual"))
        raw_steps = item.payload.get("steps", [])
        steps = [str(step) for step in raw_steps] if isinstance(raw_steps, list) else []
        settings = RuntimeSettings.model_validate(self._store.read_settings())

        if "find_sources" in steps:
            self._logged(item.run_id, trigger, "find_sources", self._find_sources, settings)
        if "scrape" in steps:
            self._logged(item.run_id, trigger, "scrape", self._scrape)

        job_steps = [step for step in JOB_STEPS if step in steps]
        if not job_steps:
            return
        self._require_ready(job_steps)
        eligible = {_STEP_STATUSES[step] for step in job_steps}
        for job_id in self._store.job_ids_with_status(eligible):
            self._run_job(item.run_id, job_id, trigger, job_steps, settings)

    def run_job(self, item: QueueItem) -> None:
        if item.job_id is None:
            raise ValueError("run_job queue item has no job_id")
        settings = RuntimeSettings.model_validate(self._store.read_settings())
        self._require_ready(JOB_STEPS)
        trigger = str(item.payload.get("trigger", "manual"))
        self._run_job(item.run_id, item.job_id, trigger, JOB_STEPS, settings)

    # --- batch steps ---------------------------------------------------------

    def _logged(
        self,
        run_id: UUID,
        trigger: str,
        step: str,
        action: Callable[..., str | None],
        *args: Any,
    ) -> None:
        started = perf_counter()
        try:
            error = action(*args)
        except Exception as exc:  # A failing batch step must not stop later steps.
            error = f"{type(exc).__name__}: {exc}"
            status = "failed"
        else:
            status = "completed_with_errors" if error else "completed"
        self._store.write_log(
            run_id=run_id,
            trigger=trigger,
            step=step,
            job_id=None,
            status=status,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
            error=error,
        )

    def _find_sources(self, settings: RuntimeSettings) -> None:
        filters = self._store.active_filters()
        remotes = {search_filter.remote for search_filter in filters}
        criteria = DiscoveryCriteria(
            roles=tuple(dict.fromkeys(role for f in filters for role in f.roles)),
            locations=tuple(dict.fromkeys(loc for f in filters for loc in f.locations)),
            remote=remotes.pop() if len(remotes) == 1 else None,
        )
        self._finder.run(
            configured_types=[SourceType(value) for value in settings.source_finder_types],
            criteria=criteria,
        )

    def _scrape(self) -> str | None:
        """Scrape every active source; per-source failures are collected, not raised."""

        filters = self._store.active_filters()
        if not filters:
            return "No active search filters"
        errors: list[str] = []
        for source in self._store.active_sources():
            try:
                cached: list[JobListing] | None = None
                for search_filter in filters:
                    if depends_on_filter(source):
                        listings = self._dispatcher.fetch(source, search_filter)
                    else:
                        if cached is None:
                            cached = self._dispatcher.fetch(source, search_filter)
                        listings = cached
                    self._scraper.save_matches(
                        source_id=source.id,
                        listings=listings,
                        search_filter=search_filter,
                    )
            except Exception as exc:
                errors.append(f"{source.name}: {type(exc).__name__}: {exc}")
        return "; ".join(errors) or None

    # --- per-job steps -------------------------------------------------------

    def _require_ready(self, job_steps: Iterable[str]) -> None:
        missing = [
            step
            for step in job_steps
            if (step == "score" and self._scorer is None)
            or (step == "tailor" and self._tailor is None)
        ]
        if missing:
            raise PipelineNotReady(
                f"LLM transport is not configured; cannot run {', '.join(missing)}"
            )
        try:
            self._base_cv.ensure_compiled()
        except LookupError as exc:
            raise PipelineNotReady("Save a base CV on the dashboard first") from exc

    def _run_job(
        self,
        run_id: UUID,
        job_id: int,
        trigger: str,
        job_steps: Sequence[str],
        settings: RuntimeSettings,
    ) -> None:
        base = self._base_cv.ensure_compiled()
        steps = [self._pipeline_step(step, base, settings) for step in job_steps]
        runner = self._runner_factory(steps)
        result = runner.run(run_id=run_id, job_id=job_id, trigger=trigger)
        if result == RunResult.ALREADY_RUNNING:
            raise PipelineBusy("Another pipeline run is in progress")

    def _pipeline_step(
        self, step: str, base: CVVersion, settings: RuntimeSettings
    ) -> PipelineStep:
        if step == "score":
            return PipelineStep(
                "score",
                frozenset({JobStatus.FOUND}),
                lambda job_id: self._score(job_id, base, settings),
            )
        return PipelineStep(
            "tailor",
            frozenset({JobStatus.SCORED}),
            lambda job_id: self._tailor_job(job_id, base, settings),
        )

    def _score(self, job_id: int, base: CVVersion, settings: RuntimeSettings) -> StepOutcome:
        assert self._scorer is not None
        job = self._store.get_job(job_id)
        filters = [
            search_filter.model_dump() for search_filter in self._store.active_filters()
        ]
        decision = self._scorer.run(
            job_id=job_id,
            job_description=job.description,
            base_cv_text=latex_to_text(base.tex),
            filters={"search_filters": filters},
            threshold=settings.score_threshold,
        )
        return StepOutcome(decision.status)

    def _tailor_job(
        self, job_id: int, base: CVVersion, settings: RuntimeSettings
    ) -> StepOutcome:
        assert self._tailor is not None
        job = self._store.get_job(job_id)
        if job.score is None:
            return StepOutcome(JobStatus.FAILED, "Job has no score for the tailor")
        decision = self._tailor.run(
            job_id=job_id,
            job_description=job.description,
            job_score=job.score,
            base_cv=base,
            settings=TailorSettings(
                skip_threshold=settings.tailor_skip_threshold,
                max_skill_days=settings.max_skill_days,
                max_added_skills=settings.max_added_skills,
                placement=settings.skill_placement,
            ),
        )
        return StepOutcome(decision.status, decision.error)
