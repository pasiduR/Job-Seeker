import json
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.llm.schemas import ScorerOutput
from app.queue.pipeline_runner import PipelineRunner, StepOutcome
from app.queue.postgres import QueueItem
from app.queue.state_machine import JobStatus
from app.queue.tasks import PipelineBusy, PipelineNotReady, PipelineTasks, WorkerJob
from app.sources.dispatch import SourceDispatcher
from app.sources.finder import PublicSourceCatalog, SourceFinder
from app.sources.models import Source, SourceCreate, SourceType
from app.sources.scraper import ScraperService, SearchFilter
from app.sources.types import JobListing
from app.steps.base_cv import BaseCVService, CVVersion
from app.steps.scorer import Scorer
from app.steps.tailor import Tailor
from tests.test_tailor import (
    FixtureTailorClient,
    MemoryTailoredCVStore,
    tailor_output,
)


RUN_ID = UUID("0b8c2f8e-3c1d-4b8a-9d61-2f9e7a6c5b40")
NOW = datetime(2026, 10, 1, 9, 0)


class MemoryWorld:
    """Worker store, pipeline store, job store, and source writer in one fake DB."""

    def __init__(self, sources: list[Source], filters: list[SearchFilter]) -> None:
        self.sources = sources
        self.filters = filters
        self.jobs: dict[int, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {"score_threshold": 7, "tailor_skip_threshold": 9}
        self.logs: list[dict[str, Any]] = []
        self.lock_available = True
        self.created_sources: list[SourceCreate] = []
        self.base: CVVersion | None = None

    # WorkerStore
    def active_sources(self) -> list[Source]:
        return [source for source in self.sources if source.active]

    def active_filters(self) -> list[SearchFilter]:
        return self.filters

    def job_ids_with_status(self, statuses: Collection[JobStatus]) -> list[int]:
        return [job_id for job_id, job in self.jobs.items() if job["status"] in statuses]

    def get_job(self, job_id: int) -> WorkerJob:
        job = self.jobs[job_id]
        return WorkerJob(
            id=job_id, description=job["description"], score=job["score"], url=job["url"],
            application_lane=job.get("application_lane", "batch"), source_id=job.get("source_id")
        )

    def get_profile(self) -> dict[str, Any]:
        return {"first_name": "Jane"}

    def cv_for_job(self, job_id: int) -> CVVersion | None:
        return None

    def read_settings(self) -> dict[str, Any]:
        return dict(self.settings)

    def write_log(self, **values: Any) -> None:
        self.logs.append(values)

    # PipelineStore
    def try_acquire_lock(self) -> bool:
        return self.lock_available

    def release_lock(self) -> None:
        pass

    def is_finished(self, run_id: UUID, job_id: int) -> bool:
        return any(
            log["run_id"] == run_id and log["job_id"] == job_id and log["step"] == "pipeline"
            for log in self.logs
        )

    def get_status(self, job_id: int) -> JobStatus:
        return self.jobs[job_id]["status"]

    def set_status(self, job_id: int, expected: JobStatus, target: JobStatus) -> None:
        assert self.jobs[job_id]["status"] == expected
        self.jobs[job_id]["status"] = target

    # JobStore
    def save_found(self, source_id: int, listing: JobListing) -> bool:
        if any(job["url"] == listing.url for job in self.jobs.values()):
            return False
        job_id = len(self.jobs) + 1
        self.jobs[job_id] = {
            "url": listing.url,
            "title": listing.title,
            "description": listing.description,
            "status": JobStatus.FOUND,
            "score": None,
        }
        return True

    # SourceWriter
    def create(self, values: SourceCreate) -> tuple[Source, bool]:
        self.created_sources.append(values)
        return _source(len(self.created_sources) + 100, values), True

    # BaseCVStore
    def get_base(self) -> CVVersion | None:
        return self.base

    def save_base(self, tex: str, pdf_path: str) -> CVVersion:
        self.base = CVVersion(id=1, tex=tex, pdf_path=pdf_path)
        return self.base

    # ScoreStore
    def save_score(self, job_id: int, score: ScorerOutput) -> None:
        self.jobs[job_id]["score"] = score.score


def _source(source_id: int, values: SourceCreate) -> Source:
    return Source(id=source_id, created_at=NOW, updated_at=NOW, **values.model_dump())


class FixtureBoards:
    def __init__(self, listings: list[JobListing]) -> None:
        self.listings = listings
        self.calls = 0

    def remotive(self) -> list[JobListing]:
        self.calls += 1
        return self.listings

    def remote_ok(self) -> list[JobListing]:
        raise RuntimeError("RemoteOK unavailable")

    def arbeitnow(self) -> list[JobListing]:
        return []

    def feed(self, *, url: str, default_company: str) -> list[JobListing]:
        return []


class SequencedScorerClient:
    def __init__(self, scores: dict[str, int]) -> None:
        self.scores = scores

    def generate(self, **kwargs: Any) -> ScorerOutput:
        description = kwargs["untrusted_data"]["job_description"]
        return ScorerOutput(score=self.scores[description], reasons=["fixture"], missing_skills=[])


class FixtureFill:
    """Fills every job except those whose URL contains "blocked"."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, int]] = []

    def run(self, *, job_id: int, job_url: str, profile: dict[str, Any], cv: CVVersion) -> StepOutcome:
        self.calls.append((job_id, job_url, cv.id))
        if "blocked" in job_url:
            return StepOutcome(JobStatus.NEEDS_MANUAL, "CAPTCHA detected")
        return StepOutcome(JobStatus.FILLED)


class FixtureSubmit:
    def __init__(self, outcome: JobStatus = JobStatus.SUBMITTED, error: str | None = None) -> None:
        self.outcome = StepOutcome(outcome, error)
        self.calls: list[tuple[int, int, str]] = []

    def run(self, *, job_id: int, daily_cap: int, lane: str = "batch") -> StepOutcome:
        self.calls.append((job_id, daily_cap, lane))
        return self.outcome


class FixtureCompiler:
    def compile(self, tex: str) -> bytes:
        return b"%PDF-1.7"


@pytest.fixture
def listings(project_root: Path) -> list[JobListing]:
    records = json.loads((project_root / "tests/fixtures/worker_listings.json").read_text(encoding="utf-8"))
    return [JobListing.model_validate(record) for record in records]


def make_tasks(
    world: MemoryWorld,
    boards: FixtureBoards,
    tmp_path: Path,
    project_root: Path,
    *,
    with_llm: bool = True,
    filler: FixtureFill | None = None,
    submitter: FixtureSubmit | None = None,
) -> PipelineTasks:
    base_tex = (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")
    base_cv = BaseCVService(store=world, compiler=FixtureCompiler(), storage_dir=tmp_path)
    base_cv.save(base_tex)
    scorer = tailor = None
    if with_llm:
        scorer = Scorer(
            llm=SequencedScorerClient({"Build Python APIs with PostgreSQL.": 8, "Design Figma prototypes.": 3}),
            store=world,
            model="fixture",
        )
        outputs = [tailor_output(project_root, base_tex, "reordered") for _ in range(3)]
        tailor = Tailor(
            llm=FixtureTailorClient(outputs),
            compiler=FixtureCompiler(),
            store=MemoryTailoredCVStore(),
            model="fixture",
            storage_dir=tmp_path,
        )
    return PipelineTasks(
        store=world,
        finder=SourceFinder(world, [PublicSourceCatalog()]),
        dispatcher=SourceDispatcher(boards=boards, ats=None, jobspy_results_wanted=5),  # type: ignore[arg-type]
        scraper=ScraperService(world),
        base_cv=base_cv,
        runner_factory=lambda steps: PipelineRunner(world, steps),
        submitter=submitter or FixtureSubmit(),  # type: ignore[arg-type]
        scorer=scorer,
        tailor=tailor,
        filler=(filler or FixtureFill()) if with_llm else None,  # type: ignore[arg-type]
    )


def remote_sources() -> list[Source]:
    return [
        _source(1, SourceCreate(name="Remotive", type=SourceType.JOB_BOARD, url="https://remotive.com", config={"adapter": "remotive"})),
        _source(2, SourceCreate(name="RemoteOK", type=SourceType.JOB_BOARD, url="https://remoteok.com", config={"adapter": "remoteok"})),
    ]


def item(task: str, payload: dict[str, Any], job_id: int | None = None) -> QueueItem:
    return QueueItem(id=1, run_id=RUN_ID, job_id=job_id, task=task, payload=payload, attempts=1)


def test_full_pipeline_finds_scrapes_scores_and_tailors(
    tmp_path: Path, project_root: Path, listings: list[JobListing]
) -> None:
    world = MemoryWorld(remote_sources(), [SearchFilter(exclude_keywords=("senior",))])
    boards = FixtureBoards(listings)
    tasks = make_tasks(world, boards, tmp_path, project_root)

    tasks.run_pipeline(
        item("run_pipeline", {"steps": ["find_sources", "scrape", "score", "tailor"], "trigger": "manual"})
    )

    assert [source.type for source in world.created_sources] == [SourceType.JOB_BOARD] * 5
    statuses = {job["title"]: job["status"] for job in world.jobs.values()}
    assert statuses == {"Python Engineer": JobStatus.TAILORED, "Product Designer": JobStatus.SKIPPED}
    batch_logs = {log["step"]: log for log in world.logs if log["job_id"] is None}
    assert batch_logs["find_sources"]["status"] == "completed"
    assert batch_logs["scrape"]["status"] == "completed_with_errors"
    assert "RemoteOK: RuntimeError" in batch_logs["scrape"]["error"]
    assert all(log["trigger"] == "manual" for log in world.logs)


def test_single_step_runs_only_that_step(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld(remote_sources()[:1], [SearchFilter(exclude_keywords=("senior",))])
    tasks = make_tasks(world, FixtureBoards(listings), tmp_path, project_root)

    tasks.run_pipeline(item("run_pipeline", {"steps": ["scrape"]}))
    assert {job["status"] for job in world.jobs.values()} == {JobStatus.FOUND}

    tasks.run_pipeline(item("run_pipeline", {"steps": ["score"]}))
    assert {job["status"] for job in world.jobs.values()} == {JobStatus.SCORED, JobStatus.SKIPPED}


def test_filter_independent_sources_are_fetched_once(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld(remote_sources()[:1], [SearchFilter(roles=("Python",)), SearchFilter(roles=("Designer",))])
    boards = FixtureBoards(listings)
    make_tasks(world, boards, tmp_path, project_root).run_pipeline(item("run_pipeline", {"steps": ["scrape"]}))

    assert boards.calls == 1
    assert sorted(job["title"] for job in world.jobs.values()) == [
        "Product Designer",
        "Python Engineer",
        "Senior Python Engineer",
    ]


def test_run_job_advances_one_job(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld([], [SearchFilter()])
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root)
    world.save_found(1, listings[0])

    tasks.run_job(item("run_job", {"trigger": "manual"}, job_id=1))

    assert world.jobs[1]["status"] == JobStatus.FILLED
    assert world.jobs[1]["score"] == 8


def test_llm_steps_fail_the_queue_item_not_the_jobs(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld([], [SearchFilter()])
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, with_llm=False)
    world.save_found(1, listings[0])

    with pytest.raises(PipelineNotReady, match="LLM transport"):
        tasks.run_job(item("run_job", {}, job_id=1))
    assert world.jobs[1]["status"] == JobStatus.FOUND


def test_missing_base_cv_fails_the_queue_item(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld([], [SearchFilter()])
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root)
    world.base = None
    world.save_found(1, listings[0])

    with pytest.raises(PipelineNotReady, match="base CV"):
        tasks.run_pipeline(item("run_pipeline", {"steps": ["score"]}))
    assert world.jobs[1]["status"] == JobStatus.FOUND


def test_busy_pipeline_is_retried_by_the_queue(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = MemoryWorld([], [SearchFilter()])
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root)
    world.save_found(1, listings[0])
    world.lock_available = False

    with pytest.raises(PipelineBusy):
        tasks.run_job(item("run_job", {}, job_id=1))


def test_needs_manual_job_does_not_stop_the_next_job(
    tmp_path: Path, project_root: Path, listings: list[JobListing]
) -> None:
    world = MemoryWorld([], [SearchFilter()])
    filler = FixtureFill()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, filler=filler)
    blocked = listings[0].model_copy(update={"url": "https://jobs.example.com/blocked"})
    world.save_found(1, blocked)
    world.save_found(1, listings[1].model_copy(update={"title": "Second"}))
    for job in world.jobs.values():
        job["status"] = JobStatus.TAILORED

    tasks.run_pipeline(item("run_pipeline", {"steps": ["fill"], "trigger": "manual"}))

    assert [job["status"] for job in world.jobs.values()] == [
        JobStatus.NEEDS_MANUAL,
        JobStatus.FILLED,
    ]
    fill_logs = [log for log in world.logs if log["step"] == "fill"]
    assert fill_logs[0]["error"] == "CAPTCHA detected"
    assert [call[2] for call in filler.calls] == [1, 1]  # base CV when not tailored


def filled_world(listings: list[JobListing]) -> MemoryWorld:
    world = MemoryWorld([], [SearchFilter()])
    world.save_found(1, listings[0])
    world.jobs[1]["status"] = JobStatus.FILLED
    world.settings["batch_daily_cap"] = 4
    return world


def test_approve_submits_right_away(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = filled_world(listings)
    submitter = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submitter)

    tasks.handlers()["review_decision"](item("review_decision", {"decision": "approve", "trigger": "manual"}, job_id=1))

    assert world.jobs[1]["status"] == JobStatus.SUBMITTED
    assert submitter.calls == [(1, 4, "batch")]
    assert [log["step"] for log in world.logs if log["step"] != "pipeline"] == ["approve", "submit"]


def test_reject_skips_without_submitting(tmp_path: Path, project_root: Path, listings: list[JobListing]) -> None:
    world = filled_world(listings)
    submitter = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submitter)

    tasks.review_decision(item("review_decision", {"decision": "reject"}, job_id=1))

    assert world.jobs[1]["status"] == JobStatus.SKIPPED
    assert submitter.calls == []


@pytest.mark.parametrize("status", [JobStatus.TAILORED, JobStatus.SKIPPED, JobStatus.SUBMITTED])
def test_decisions_only_apply_to_filled_jobs(
    tmp_path: Path, project_root: Path, listings: list[JobListing], status: JobStatus
) -> None:
    world = filled_world(listings)
    world.jobs[1]["status"] = status
    submitter = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submitter)

    tasks.review_decision(item("review_decision", {"decision": "approve"}, job_id=1))

    assert world.jobs[1]["status"] == status
    assert submitter.calls == []


def test_capped_approval_stays_approved_and_a_later_run_submits(
    tmp_path: Path, project_root: Path, listings: list[JobListing]
) -> None:
    world = filled_world(listings)
    capped = FixtureSubmit(JobStatus.APPROVED, "batch daily cap of 4 reached")
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=capped)

    tasks.review_decision(item("review_decision", {"decision": "approve"}, job_id=1))
    assert world.jobs[1]["status"] == JobStatus.APPROVED

    capped.outcome = StepOutcome(JobStatus.SUBMITTED)
    later_run = QueueItem(id=2, run_id=UUID(int=2), job_id=None, task="run_pipeline",
                          payload={"steps": ["submit"]}, attempts=1)
    tasks.run_pipeline(later_run)
    assert world.jobs[1]["status"] == JobStatus.SUBMITTED


def test_submit_step_only_touches_approved_jobs(
    tmp_path: Path, project_root: Path, listings: list[JobListing]
) -> None:
    world = MemoryWorld([], [SearchFilter()])
    for index, status in enumerate([JobStatus.FILLED, JobStatus.TAILORED, JobStatus.APPROVED, JobStatus.SUBMITTED]):
        world.save_found(1, listings[0].model_copy(update={"url": f"https://jobs.example.com/{index}", "title": f"Job {index}"}))
        world.jobs[index + 1]["status"] = status
    submitter = FixtureSubmit()
    tasks = make_tasks(world, FixtureBoards([]), tmp_path, project_root, submitter=submitter)

    tasks.run_pipeline(item("run_pipeline", {"steps": ["submit"]}))

    assert [call[0] for call in submitter.calls] == [3]
    assert [job["status"] for job in world.jobs.values()] == [
        JobStatus.FILLED, JobStatus.TAILORED, JobStatus.SUBMITTED, JobStatus.SUBMITTED,
    ]
