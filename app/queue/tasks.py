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
from app.sources.dispatch import (
    SourceDispatcher,
    UnsupportedSource,
    ats_target_from,
    depends_on_filter,
)
from app.sources.finder import DiscoveryCriteria, SourceFinder
from app.sources.models import Source, SourceRepository, SourceType
from app.sources.scraper import ScraperService, SearchFilter
from app.sources.types import JobListing
from app.steps.base_cv import BaseCVService, CVVersion
from app.steps.auto_submit import AutoSubmitFacts, can_auto_submit
from app.steps.fill import FillService, application_form_url
from app.steps.submit import Lane, SubmitService
from app.steps.latex import latex_to_text
from app.steps.scorer import Scorer
from app.steps.tailor import Tailor, TailorSettings
from app.triggers.manual import RUN_JOB_TASK, RUN_PIPELINE_TASK
from app.triggers.review import REVIEW_DECISION_TASK, Decision
from app.triggers.watcher import POLL_SUBSCRIPTION_TASK, Watcher


JOB_STEPS = ("score", "tailor", "fill", "submit")
_STEP_STATUSES = {
    "score": JobStatus.FOUND,
    "tailor": JobStatus.SCORED,
    "fill": JobStatus.TAILORED,
    "submit": JobStatus.APPROVED,
}


class PipelineBusy(RuntimeError):
    """Another run holds the pipeline lock; the queue retries later."""


class PipelineNotReady(RuntimeError):
    """A prerequisite (LLM transport, base CV) is missing; jobs stay untouched."""


@dataclass(frozen=True)
class WorkerJob:
    id: int
    description: str
    score: int | None
    url: str = ""
    form_url: str = ""
    application_lane: Lane = "batch"
    source_id: int | None = None


class WorkerStore(Protocol):
    def auto_submit_facts(self, job_id: int) -> AutoSubmitFacts | None: ...

    def active_sources(self) -> list[Source]: ...

    def active_filters(self) -> list[SearchFilter]: ...

    def job_ids_with_status(self, statuses: Collection[JobStatus]) -> list[int]: ...

    def get_job(self, job_id: int) -> WorkerJob: ...

    def get_profile(self) -> dict[str, Any]: ...

    def cv_for_job(self, job_id: int) -> CVVersion | None: ...

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
            """
            SELECT j.id, j.description, j.score, j.url, j.source_job_id,
                   s.type, s.url, s.config, s.name, j.application_lane, j.source_id
            FROM jobs j LEFT JOIN sources s ON s.id = j.source_id
            WHERE j.id = %s
            """,
            (job_id,),
        )
        if not rows:
            raise LookupError(f"Job {job_id} does not exist")
        row = rows[0]
        ats: tuple[str, str] | None = None
        if row[5] == SourceType.ATS_BOARD.value:
            try:
                ats = ats_target_from(str(row[6]), row[7] or {}, name=str(row[8]))
            except UnsupportedSource:
                ats = None
        return WorkerJob(
            id=int(row[0]),
            description=str(row[1]),
            score=row[2],
            url=str(row[3]),
            application_lane=row[9],
            source_id=row[10],
            form_url=application_form_url(
                str(row[3]), source_job_id=row[4], ats=ats
            ),
        )

    def get_profile(self) -> dict[str, Any]:
        rows = self._rows("SELECT data FROM profile WHERE id = 1")
        return dict(rows[0][0]) if rows else {}

    def auto_submit_facts(self, job_id: int) -> AutoSubmitFacts | None:
        rows = self._rows("""
            SELECT j.score, j.source_id, f.answers, f.outcome,
                   (SELECT count(*) FROM skills_to_learn WHERE job_id = j.id)
            FROM jobs j JOIN form_fills f ON f.job_id = j.id WHERE j.id = %s
        """, (job_id,))
        if not rows:
            return None
        import json
        score, source_id, answers, outcome, count = rows[0]
        return AutoSubmitFacts(score=score, source_id=source_id,
                               answers=json.loads(answers) if isinstance(answers, str) else answers,
                               outcome=outcome, added_skill_count=count)

    def cv_for_job(self, job_id: int) -> CVVersion | None:
        """The job's tailored CV; None when it reuses the base CV."""

        rows = self._rows(
            "SELECT id, tex, pdf_path FROM cv_versions WHERE job_id = %s", (job_id,)
        )
        if not rows:
            return None
        return CVVersion(id=int(rows[0][0]), tex=str(rows[0][1]), pdf_path=str(rows[0][2]))

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

    ``scorer``, ``tailor``, and ``filler`` are None when ANTHROPIC_API_KEY is
    missing; LLM steps then fail the queue item, never the jobs.
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
        submitter: SubmitService,
        scorer: Scorer | None = None,
        tailor: Tailor | None = None,
        filler: FillService | None = None,
        watcher: Watcher | None = None,
    ) -> None:
        self._store = store
        self._finder = finder
        self._dispatcher = dispatcher
        self._scraper = scraper
        self._base_cv = base_cv
        self._runner_factory = runner_factory
        self._submitter = submitter
        self._scorer = scorer
        self._tailor = tailor
        self._filler = filler
        self._watcher = watcher

    def handlers(self) -> dict[str, Callable[[QueueItem], None]]:
        return {
            RUN_PIPELINE_TASK: self.run_pipeline,
            RUN_JOB_TASK: self.run_job,
            REVIEW_DECISION_TASK: self.review_decision,
            POLL_SUBSCRIPTION_TASK: self.poll_subscription,
        }

    def poll_subscription(self, item: QueueItem) -> None:
        if self._watcher is None:
            raise PipelineNotReady("Watcher is not configured")
        # Let queue retry transient poll failures, but record each failed attempt.
        started = perf_counter()
        error = None
        try:
            self._watcher.poll(item)
        except Exception as exc:
            error = f"Subscription poll failed: {type(exc).__name__}"
            raise
        finally:
            self._store.write_log(run_id=item.run_id, trigger="event", step="watch",
                                  job_id=None, status="failed" if error else "completed",
                                  duration_ms=max(0, round((perf_counter() - started) * 1000)), error=error)

    def review_decision(self, item: QueueItem) -> None:
        """Approve (then submit right away) or reject one filled job."""

        if item.job_id is None:
            raise ValueError("review_decision queue item has no job_id")
        decision = Decision(str(item.payload.get("decision")))
        settings = RuntimeSettings.model_validate(self._store.read_settings())
        target = JobStatus.APPROVED if decision == Decision.APPROVE else JobStatus.SKIPPED
        steps = [
            PipelineStep(
                decision.value,
                frozenset({JobStatus.FILLED}),
                lambda job_id: StepOutcome(target),
            )
        ]
        if decision == Decision.APPROVE:
            steps.append(self._submit_step(settings))
        runner = self._runner_factory(steps)
        trigger = str(item.payload.get("trigger", "manual"))
        result = runner.run(run_id=item.run_id, job_id=item.job_id, trigger=trigger)
        if result == RunResult.ALREADY_RUNNING:
            raise PipelineBusy("Another pipeline run is in progress")

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
            or (step == "fill" and self._filler is None)
        ]
        if missing:
            raise PipelineNotReady(
                "LLM transport is not configured (set ANTHROPIC_API_KEY in .env); "
                f"cannot run {', '.join(missing)}"
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
        if "fill" in job_steps and settings.auto_submit:
            fill_index = next(i for i, step in enumerate(steps) if step.name == "fill")
            steps.insert(fill_index + 1, PipelineStep(
                "auto_approve", frozenset({JobStatus.FILLED}),
                lambda current_job_id: StepOutcome(
                    JobStatus.APPROVED if can_auto_submit(self._store.auto_submit_facts(current_job_id), settings)
                    else JobStatus.FILLED
                ),
            ))
            # A fill-only request still submits immediately when auto-approval is enabled.
            if "submit" not in job_steps:
                steps.append(self._submit_step(settings))
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
        if step == "submit":
            return self._submit_step(settings)
        if step == "tailor":
            return PipelineStep(
                "tailor",
                frozenset({JobStatus.SCORED}),
                lambda job_id: self._tailor_job(job_id, base, settings),
            )
        return PipelineStep(
            "fill",
            frozenset({JobStatus.TAILORED}),
            lambda job_id: self._fill_job(job_id, base),
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

    def _fill_job(self, job_id: int, base: CVVersion) -> StepOutcome:
        assert self._filler is not None
        job = self._store.get_job(job_id)
        return self._filler.run(
            job_id=job_id,
            job_url=job.form_url or job.url,
            profile=self._store.get_profile(),
            cv=self._store.cv_for_job(job_id) or base,
        )

    def _submit_step(self, settings: RuntimeSettings) -> PipelineStep:
        return PipelineStep(
            "submit",
            frozenset({JobStatus.APPROVED}),
            lambda job_id: self._submit_job(job_id, settings),
        )

    def _submit_job(self, job_id: int, settings: RuntimeSettings) -> StepOutcome:
        lane = self._store.get_job(job_id).application_lane
        cap = settings.fast_lane_daily_cap if lane == "fast_lane" else settings.batch_daily_cap
        return self._submitter.run(job_id=job_id, daily_cap=cap, lane=lane)
