import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.dashboard.app import DashboardCredentials, create_app
from app.dashboard.pages import router
from app.dashboard.repository import (
    DashboardRepos,
    JobRow,
    PostgresDashboardStore,
    RunLogRow,
    SearchFilterCreate,
    SearchFilterRow,
    SkillRow,
)
from app.sources.models import Source, SourceCreate
from app.steps.base_cv import BaseCVService, CVVersion
from app.steps.latex import LatexCompileError
from app.triggers.manual import ManualTrigger
from tests.test_manual_trigger import MemoryTriggerQueue


AUTH = ("admin", "fixture-password")
ORIGIN = {"origin": "http://testserver"}
NOW = datetime(2026, 10, 1, 9, 0)


class MemoryDashboardStore:
    """In-memory DashboardStore that also serves as the BaseCVStore."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.sources: dict[int, Source] = {}
        self.filters: dict[int, SearchFilterRow] = {}
        self.profile: dict[str, Any] = {}
        self.base: CVVersion | None = None
        self.jobs = [JobRow(**{**job, "created_at": datetime.fromisoformat(job["created_at"])}) for job in data["jobs"]]
        self.skills = [SkillRow(**skill) for skill in data["skills"]]
        self.logs = [RunLogRow(**{**log, "created_at": datetime.fromisoformat(log["created_at"])}) for log in data["run_logs"]]
        self.settings: dict[str, Any] = dict(data["settings"])
        self.job_queries: list[dict[str, Any]] = []

    def list_sources(self) -> list[Source]:
        return list(self.sources.values())

    def create_source(self, values: SourceCreate) -> tuple[Source, bool]:
        for source in self.sources.values():
            if (source.type, source.url) == (values.type, values.url):
                return source, False
        source = Source(
            id=len(self.sources) + 1, created_at=NOW, updated_at=NOW, **values.model_dump()
        )
        self.sources[source.id] = source
        return source, True

    def set_source_active(self, source_id: int, active: bool) -> bool:
        if source_id not in self.sources:
            return False
        self.sources[source_id] = self.sources[source_id].model_copy(update={"active": active})
        return True

    def delete_source(self, source_id: int) -> bool:
        return self.sources.pop(source_id, None) is not None

    def list_filters(self) -> list[SearchFilterRow]:
        return list(self.filters.values())

    def create_filter(self, values: SearchFilterCreate) -> bool:
        if any(row.name == values.name for row in self.filters.values()):
            return False
        row = SearchFilterRow(id=len(self.filters) + 1, active=True, **values.model_dump())
        self.filters[row.id] = row
        return True

    def set_filter_active(self, filter_id: int, active: bool) -> bool:
        row = self.filters.get(filter_id)
        if row is None:
            return False
        self.filters[filter_id] = SearchFilterRow(**{**row.__dict__, "active": active})
        return True

    def delete_filter(self, filter_id: int) -> bool:
        return self.filters.pop(filter_id, None) is not None

    def get_profile(self) -> dict[str, Any]:
        return dict(self.profile)

    def save_profile(self, data: Mapping[str, Any]) -> None:
        self.profile = dict(data)

    def get_base(self) -> CVVersion | None:
        return self.base

    def save_base(self, tex: str, pdf_path: str) -> CVVersion:
        self.base = CVVersion(id=1, tex=tex, pdf_path=pdf_path)
        return self.base

    def list_jobs(self, *, status: str | None, min_score: int | None, limit: int) -> list[JobRow]:
        self.job_queries.append({"status": status, "min_score": min_score, "limit": limit})
        return [
            job
            for job in self.jobs
            if (status is None or job.status == status)
            and (min_score is None or (job.score or 0) >= min_score)
        ][:limit]

    def list_skills(self) -> list[SkillRow]:
        return self.skills

    def list_run_logs(self, *, limit: int) -> list[RunLogRow]:
        return self.logs[:limit]

    def read_settings(self) -> dict[str, Any]:
        return dict(self.settings)

    def save_settings(self, values: Mapping[str, Any]) -> None:
        self.settings.update(values)


class FixtureCompiler:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def compile(self, tex: str) -> bytes:
        if self.error:
            raise self.error
        return b"%PDF-1.7 base"


@pytest.fixture
def store(project_root: Path) -> MemoryDashboardStore:
    data = json.loads((project_root / "tests/fixtures/dashboard_data.json").read_text(encoding="utf-8"))
    return MemoryDashboardStore(data)


def make_client(
    store: MemoryDashboardStore,
    tmp_path: Path,
    compiler: FixtureCompiler | None = None,
    queue: MemoryTriggerQueue | None = None,
) -> TestClient:
    trigger = ManualTrigger(queue or MemoryTriggerQueue())

    @contextmanager
    def factory() -> Iterator[DashboardRepos]:
        yield DashboardRepos(
            store=store,
            base_cv=BaseCVService(store=store, compiler=compiler or FixtureCompiler(), storage_dir=tmp_path),
            trigger=trigger,
        )

    app = create_app(
        repo_factory=factory,
        credentials=DashboardCredentials(username=AUTH[0], password=SecretStr(AUTH[1])),
        routers=(router,),
    )
    return TestClient(app, follow_redirects=False)


def post(client: TestClient, path: str, data: Mapping[str, Any]):
    return client.post(path, data=data, auth=AUTH, headers=ORIGIN)


@pytest.mark.parametrize("path", ["/sources", "/filters", "/cv", "/jobs", "/skills", "/runs", "/settings"])
def test_every_page_renders_behind_auth(store: MemoryDashboardStore, tmp_path: Path, path: str) -> None:
    client = make_client(store, tmp_path)

    assert client.get(path).status_code == 401
    response = client.get(path, auth=AUTH)
    assert response.status_code == 200
    assert "<nav>" in response.text


def test_sources_can_be_added_toggled_and_removed(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    response = post(client, "/sources", {"name": "Acme", "type": "ats_board", "url": "https://boards.greenhouse.io/acme"})
    assert response.status_code == 303
    assert "Source+added" in response.headers["location"]
    assert "already+exists" in post(client, "/sources", {"name": "Acme", "type": "ats_board", "url": "https://boards.greenhouse.io/acme"}).headers["location"]

    post(client, "/sources/1/active", {"active": "false"})
    assert store.sources[1].active is False
    page = client.get("/sources", auth=AUTH)
    assert "Acme" in page.text and "disabled" in page.text

    post(client, "/sources/1/delete", {})
    assert store.sources == {}


def test_source_config_rejects_secrets_and_bad_json(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    secret = post(client, "/sources", {"name": "A", "type": "rss", "url": "https://a.example/feed", "config": '{"api_key": "x"}'})
    bad_json = post(client, "/sources", {"name": "A", "type": "rss", "url": "https://a.example/feed", "config": "{"})

    assert "error=" in secret.headers["location"]
    assert "JSON+object" in bad_json.headers["location"]
    assert store.sources == {}


def test_search_filters_are_parsed_from_comma_lists(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    post(client, "/filters", {"name": "Remote Python", "roles": "Python Engineer, Backend Engineer", "locations": "", "remote": "yes", "exclude_keywords": "senior,  lead"})
    duplicate = post(client, "/filters", {"name": "Remote Python"})
    post(client, "/filters/1/active", {"active": "false"})

    row = store.filters[1]
    assert row.roles == ["Python Engineer", "Backend Engineer"]
    assert row.remote is True
    assert row.exclude_keywords == ["senior", "lead"]
    assert row.active is False
    assert "already+exists" in duplicate.headers["location"]


def test_base_cv_is_saved_compiled_and_downloadable(store: MemoryDashboardStore, tmp_path: Path, project_root: Path) -> None:
    client = make_client(store, tmp_path)
    tex = (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")

    response = post(client, "/cv/base", {"tex": tex})

    assert "compiled" in response.headers["location"]
    assert store.base is not None and store.base.tex == tex
    page = client.get("/cv", auth=AUTH)
    assert "Northwind Logistics" in page.text
    assert "/cv/base.pdf" in page.text
    assert client.get("/cv/base.pdf", auth=AUTH).content == b"%PDF-1.7 base"


def test_base_cv_compile_errors_are_shown_and_not_saved(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path, FixtureCompiler(LatexCompileError("pdflatex exited with status 1", "! Undefined control sequence.")))

    response = post(client, "/cv/base", {"tex": "\\documentclass{article}\\bad"})

    assert "did+not+compile" in response.headers["location"]
    assert store.base is None


def test_profile_must_be_a_json_object(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    assert "error=" in post(client, "/cv/profile", {"profile": "[1, 2]"}).headers["location"]
    assert "error=" in post(client, "/cv/profile", {"profile": "{oops"}).headers["location"]
    post(client, "/cv/profile", {"profile": '{"full_name": "Alex Example", "visa_sponsorship": false}'})

    assert store.profile == {"full_name": "Alex Example", "visa_sponsorship": False}


def test_jobs_page_filters_by_status_and_score(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    response = client.get("/jobs?status=scored&min_score=7", auth=AUTH)

    assert store.job_queries[-1] == {"status": "scored", "min_score": 7, "limit": 200}
    assert "Backend Engineer" in response.text
    assert "Data Analyst" not in response.text
    client.get("/jobs?status=bogus&min_score=x", auth=AUTH)
    assert store.job_queries[-1]["status"] is None
    assert store.job_queries[-1]["min_score"] is None


def test_untrusted_job_urls_are_not_linked(store: MemoryDashboardStore, tmp_path: Path) -> None:
    response = make_client(store, tmp_path).get("/jobs", auth=AUTH)

    assert "javascript:" not in response.text
    assert 'href="https://jobs.example.com/1"' in response.text


def test_skills_and_run_logs_pages_list_rows(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)

    assert "Add caching to a side project." in client.get("/skills", auth=AUTH).text
    runs = client.get("/runs", auth=AUTH).text
    assert "8d6f0c3e" in runs and "schema validation twice" in runs


def test_settings_are_validated_before_saving(store: MemoryDashboardStore, tmp_path: Path) -> None:
    client = make_client(store, tmp_path)
    page = client.get("/settings", auth=AUTH).text
    assert 'name="tailor_skip_threshold"' in page

    form = {
        "score_threshold": "8", "tailor_skip_threshold": "9", "max_skill_days": "5",
        "max_added_skills": "2", "skill_placement": "skills_section",
        "batch_daily_cap": "10", "fast_lane_daily_cap": "5",
    }
    assert "saved" in post(client, "/settings", form).headers["location"]
    assert store.settings["score_threshold"] == 8
    assert store.settings["skill_placement"] == "skills_section"
    assert store.settings["auto_submit"] is False

    rejected = post(client, "/settings", {**form, "score_threshold": "11"})
    assert "error=" in rejected.headers["location"]
    assert store.settings["score_threshold"] == 8


class RecordingConnection:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[tuple[Any, ...]]:
        self.statements.append((" ".join(query.split()), params))
        return self.rows


def test_postgres_store_builds_parameterised_job_query() -> None:
    connection = RecordingConnection([(1, "T", "C", "https://x", None, "found", None, NOW)])
    jobs = PostgresDashboardStore(connection).list_jobs(status="found", min_score=6, limit=50)

    query, params = connection.statements[-1]
    assert "WHERE status = %s AND score >= %s" in query
    assert params == ("found", 6, 50)
    assert jobs[0].title == "T"


def test_postgres_store_upserts_profile_and_settings_as_json() -> None:
    connection = RecordingConnection()
    store = PostgresDashboardStore(connection)

    store.save_profile({"full_name": "Alex"})
    store.save_settings({"score_threshold": 8, "skill_placement": "skills_section"})

    profile_sql, profile_params = connection.statements[0]
    assert "ON CONFLICT (id) DO UPDATE" in profile_sql
    assert profile_params == ('{"full_name": "Alex"}',)
    assert [params for _, params in connection.statements[1:]] == [
        ("score_threshold", "8"),
        ("skill_placement", '"skills_section"'),
    ]


def test_run_now_queues_full_pipeline_or_single_step(store: MemoryDashboardStore, tmp_path: Path) -> None:
    queue = MemoryTriggerQueue()
    client = make_client(store, tmp_path, queue=queue)
    assert 'action="/run"' in client.get("/", auth=AUTH).text

    full = post(client, "/run", {"step": "all", "back": "/"})
    single = post(client, "/run", {"step": "score", "back": "/jobs"})
    repeat = post(client, "/run", {"step": "score", "back": "/jobs"})
    unknown = post(client, "/run", {"step": "submit", "back": "/"})

    assert full.headers["location"].startswith("/?message=Full+pipeline+queued")
    assert single.headers["location"].startswith("/jobs?message=")
    assert "already+queued" in repeat.headers["location"]
    assert "Unknown+step" in unknown.headers["location"]
    assert [item["payload"]["steps"] for item in queue.items] == [
        ["find_sources", "scrape", "score", "tailor"],
        ["score"],
    ]


def test_run_for_this_job_is_offered_for_runnable_jobs(store: MemoryDashboardStore, tmp_path: Path) -> None:
    queue = MemoryTriggerQueue()
    client = make_client(store, tmp_path, queue=queue)

    page = client.get("/jobs", auth=AUTH).text
    assert 'action="/jobs/1/run"' in page
    assert 'action="/jobs/3/run"' in page
    assert 'action="/jobs/2/run"' not in page

    response = post(client, "/jobs/1/run", {})
    assert "Pipeline+for+job+1+queued" in response.headers["location"]
    assert queue.items[0]["task"] == "run_job" and queue.items[0]["job_id"] == 1
