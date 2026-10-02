"""Server-rendered dashboard pages for configuration and monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.config import RuntimeSettings
from app.dashboard.app import get_repos, templates
from app.dashboard.repository import DashboardRepos, SearchFilterCreate
from app.queue.state_machine import JobStatus
from app.steps.fill import SCREENSHOT_ROOT
from app.steps.form_mapper import RESUME_UPLOAD
from app.sources.models import SourceCreate, SourceType
from app.steps.latex import LatexCompileError, LatexEngineMissing
from app.triggers.manual import PIPELINE_STEPS
from app.triggers.review import Decision


RUNNABLE_STATUSES = frozenset(
    {JobStatus.FOUND.value, JobStatus.SCORED.value, JobStatus.TAILORED.value}
)


router = APIRouter()

JOBS_PAGE_LIMIT = 200
RUN_LOG_LIMIT = 200
REVIEW_PAGE_LIMIT = 50


def _redirect(path: str, *, message: str | None = None, error: str | None = None) -> Response:
    query = {key: value for key, value in (("message", message), ("error", error)) if value}
    target = f"{path}?{urlencode(query)}" if query else path
    return RedirectResponse(target, status_code=303)


def _render(request: Request, template: str, **context: Any) -> Response:
    context.setdefault("message", request.query_params.get("message"))
    context.setdefault("error", request.query_params.get("error"))
    return templates.TemplateResponse(request, template, context)


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"
        for error in exc.errors()
    )


# --- Sources -----------------------------------------------------------------


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    return _render(
        request,
        "sources.html",
        sources=repos.store.list_sources(),
        source_types=[source_type.value for source_type in SourceType],
    )


@router.post("/sources")
def create_source(
    name: str = Form(...),
    type: str = Form(...),
    url: str = Form(...),
    config: str = Form(""),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    try:
        parsed_config = json.loads(config) if config.strip() else {}
        values = SourceCreate(name=name, type=type, url=url, config=parsed_config)
    except json.JSONDecodeError:
        return _redirect("/sources", error="Config must be a JSON object")
    except ValidationError as exc:
        return _redirect("/sources", error=_validation_message(exc))
    _, created = repos.store.create_source(values)
    message = "Source added" if created else "Source already exists"
    return _redirect("/sources", message=message)


@router.post("/sources/{source_id}/active")
def set_source_active(
    source_id: int,
    active: bool = Form(...),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    if not repos.store.set_source_active(source_id, active):
        return _redirect("/sources", error="Source not found")
    return _redirect("/sources", message="Source enabled" if active else "Source disabled")


@router.post("/sources/{source_id}/delete")
def delete_source(source_id: int, repos: DashboardRepos = Depends(get_repos)) -> Response:
    if not repos.store.delete_source(source_id):
        return _redirect("/sources", error="Source not found")
    return _redirect("/sources", message="Source removed")


# --- Search filters ----------------------------------------------------------


@router.get("/filters", response_class=HTMLResponse)
def filters_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    return _render(request, "filters.html", filters=repos.store.list_filters())


@router.post("/filters")
def create_filter(
    name: str = Form(...),
    roles: str = Form(""),
    locations: str = Form(""),
    remote: Literal["any", "yes", "no"] = Form("any"),
    exclude_keywords: str = Form(""),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    try:
        values = SearchFilterCreate(
            name=name.strip(),
            roles=_split_list(roles),
            locations=_split_list(locations),
            remote={"any": None, "yes": True, "no": False}[remote],
            exclude_keywords=_split_list(exclude_keywords),
        )
    except ValidationError as exc:
        return _redirect("/filters", error=_validation_message(exc))
    if not repos.store.create_filter(values):
        return _redirect("/filters", error="A filter with that name already exists")
    return _redirect("/filters", message="Filter added")


@router.post("/filters/{filter_id}/active")
def set_filter_active(
    filter_id: int,
    active: bool = Form(...),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    if not repos.store.set_filter_active(filter_id, active):
        return _redirect("/filters", error="Filter not found")
    return _redirect("/filters", message="Filter enabled" if active else "Filter disabled")


@router.post("/filters/{filter_id}/delete")
def delete_filter(filter_id: int, repos: DashboardRepos = Depends(get_repos)) -> Response:
    if not repos.store.delete_filter(filter_id):
        return _redirect("/filters", error="Filter not found")
    return _redirect("/filters", message="Filter removed")


# --- Base CV and profile -----------------------------------------------------


@router.get("/cv", response_class=HTMLResponse)
def cv_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    base = repos.store.get_base()
    return _render(
        request,
        "cv.html",
        base=base,
        base_pdf_ready=base is not None and Path(base.pdf_path).is_file(),
        profile_json=json.dumps(repos.store.get_profile(), indent=2, sort_keys=True),
    )


@router.post("/cv/base")
def save_base_cv(
    tex: str = Form(...),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    try:
        repos.base_cv.save(tex.replace("\r\n", "\n"))
    except ValueError as exc:
        return _redirect("/cv", error=str(exc))
    except LatexEngineMissing as exc:
        return _redirect("/cv", error=str(exc))
    except LatexCompileError as exc:
        return _redirect("/cv", error=f"Base CV did not compile: {exc}. {exc.log[-500:]}")
    return _redirect("/cv", message="Base CV saved and compiled")


@router.get("/cv/base.pdf")
def base_cv_pdf(repos: DashboardRepos = Depends(get_repos)) -> Response:
    base = repos.store.get_base()
    if base is None or not Path(base.pdf_path).is_file():
        return Response("Base CV PDF not found", status_code=404)
    return FileResponse(base.pdf_path, media_type="application/pdf", filename="base-cv.pdf")


@router.post("/cv/profile")
def save_profile(
    profile: str = Form(...),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    try:
        data = json.loads(profile)
    except json.JSONDecodeError as exc:
        return _redirect("/cv", error=f"Profile is not valid JSON: {exc.msg}")
    if not isinstance(data, dict):
        return _redirect("/cv", error="Profile must be a JSON object")
    repos.store.save_profile(data)
    return _redirect("/cv", message="Profile saved")


# --- Jobs, skills, run logs --------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    status: str = "",
    min_score: str = "",
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    statuses = [job_status.value for job_status in JobStatus]
    selected_status = status if status in statuses else None
    score = int(min_score) if min_score.isdigit() else None
    return _render(
        request,
        "jobs.html",
        jobs=repos.store.list_jobs(
            status=selected_status, min_score=score, limit=JOBS_PAGE_LIMIT
        ),
        statuses=statuses,
        selected_status=selected_status or "",
        min_score=score if score is not None else "",
        pipeline_steps=PIPELINE_STEPS,
        runnable_statuses=RUNNABLE_STATUSES,
    )


# --- Review queue ------------------------------------------------------------


def _display_value(value: Any) -> str:
    if value == RESUME_UPLOAD:
        return "Tailored CV (PDF)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    return _render(
        request,
        "review.html",
        items=repos.store.list_review_items(limit=REVIEW_PAGE_LIMIT),
        display_value=_display_value,
    )


@router.get("/review/{job_id}/screenshot")
def review_screenshot(job_id: int, repos: DashboardRepos = Depends(get_repos)) -> Response:
    stored = repos.store.get_screenshot_path(job_id)
    if not stored:
        return Response("Screenshot not found", status_code=404)
    path = Path(stored).resolve()
    # Only serve files the filler wrote under the screenshot directory.
    if not path.is_relative_to(SCREENSHOT_ROOT.resolve()) or not path.is_file():
        return Response("Screenshot not found", status_code=404)
    return FileResponse(path, media_type="image/png")


@router.post("/review/{job_id}/{decision}")
def review_decision(
    job_id: int, decision: Decision, repos: DashboardRepos = Depends(get_repos)
) -> Response:
    result = repos.decisions.decide(job_id, decision)
    if result.queued:
        return _redirect("/review", message=result.message)
    return _redirect("/review", error=result.message)


# --- Manual triggers ---------------------------------------------------------


@router.post("/run")
def run_now(
    step: str = Form("all"),
    back: Literal["/", "/jobs"] = Form("/"),
    repos: DashboardRepos = Depends(get_repos),
) -> Response:
    if step != "all" and step not in PIPELINE_STEPS:
        return _redirect(back, error=f"Unknown step: {step}")
    result = repos.trigger.run_now(None if step == "all" else step)
    if result.queued:
        return _redirect(back, message=result.message)
    return _redirect(back, error=result.message)


@router.post("/jobs/{job_id}/run")
def run_for_job(job_id: int, repos: DashboardRepos = Depends(get_repos)) -> Response:
    result = repos.trigger.run_for_job(job_id)
    if result.queued:
        return _redirect("/jobs", message=result.message)
    return _redirect("/jobs", error=result.message)


@router.get("/skills", response_class=HTMLResponse)
def skills_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    return _render(request, "skills.html", skills=repos.store.list_skills())


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    return _render(request, "runs.html", logs=repos.store.list_run_logs(limit=RUN_LOG_LIMIT))


# --- Settings ----------------------------------------------------------------


def _settings_fields(values: RuntimeSettings) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for name, info in RuntimeSettings.model_fields.items():
        annotation = info.annotation
        field: dict[str, Any] = {"name": name, "value": getattr(values, name)}
        if annotation is bool:
            field["kind"] = "bool"
        elif get_origin(annotation) is list:
            field["kind"] = "list"
            field["value"] = ", ".join(field["value"])
            field["options"] = list(get_args(get_args(annotation)[0]))
        elif get_origin(annotation) is Literal:
            field["kind"] = "choice"
            field["options"] = list(get_args(annotation))
        elif annotation is str:
            field["kind"] = "text"
        else:
            field["kind"] = "number"
            field["step"] = "any" if annotation is float else "1"
        fields.append(field)
    return fields


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, repos: DashboardRepos = Depends(get_repos)) -> Response:
    current = RuntimeSettings.model_validate(repos.store.read_settings())
    return _render(request, "settings.html", fields=_settings_fields(current))


@router.post("/settings")
async def save_settings(
    request: Request, repos: DashboardRepos = Depends(get_repos)
) -> Response:
    form = await request.form()
    submitted: dict[str, Any] = {}
    for name, info in RuntimeSettings.model_fields.items():
        if info.annotation is bool:
            submitted[name] = name in form
        elif name in form and get_origin(info.annotation) is list:
            submitted[name] = _split_list(str(form[name]))
        elif name in form:
            submitted[name] = form[name]
    try:
        validated = RuntimeSettings.model_validate(
            {**repos.store.read_settings(), **submitted}
        )
    except ValidationError as exc:
        return _redirect("/settings", error=_validation_message(exc))
    repos.store.save_settings({name: getattr(validated, name) for name in submitted})
    return _redirect("/settings", message="Settings saved")
