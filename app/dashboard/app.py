"""Dashboard application factory: auth, same-origin checks, and templates."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RepoFactory = Callable[[], AbstractContextManager[Any]]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@dataclass(frozen=True)
class DashboardCredentials:
    username: str
    password: SecretStr

    def __post_init__(self) -> None:
        if not self.username or not self.password.get_secret_value():
            raise ValueError("Dashboard username and password must be set in .env")


_basic = HTTPBasic(auto_error=False)


def require_user(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    expected: DashboardCredentials = request.app.state.credentials
    if credentials is not None:
        username_ok = secrets.compare_digest(
            credentials.username.encode("utf-8"), expected.username.encode("utf-8")
        )
        password_ok = secrets.compare_digest(
            credentials.password.encode("utf-8"),
            expected.password.get_secret_value().encode("utf-8"),
        )
        if username_ok and password_ok:
            return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="job-seeker"'},
    )


def get_repos(request: Request) -> Iterator[Any]:
    """One database-backed repository bundle per request."""

    factory: RepoFactory = request.app.state.repo_factory
    with factory() as repos:
        yield repos


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts; Basic auth alone does not stop CSRF."""

    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return False
    parsed = urlsplit(source)
    return parsed.netloc == request.url.netloc and parsed.scheme == request.url.scheme


def create_app(
    *,
    repo_factory: RepoFactory,
    credentials: DashboardCredentials,
    routers: tuple[Any, ...] = (),
) -> FastAPI:
    app = FastAPI(
        title="Job Seeker dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.repo_factory = repo_factory
    app.state.credentials = credentials
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _SAFE_METHODS and not _same_origin(request):
            response: Response = JSONResponse(
                {"detail": "Cross-origin request rejected"},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        else:
            response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user: str = Depends(require_user)) -> Response:
        return templates.TemplateResponse(request, "home.html", {"user": user})

    for router in routers:
        app.include_router(router, dependencies=[Depends(require_user)])
    return app


__all__ = [
    "DashboardCredentials",
    "create_app",
    "get_repos",
    "require_user",
    "templates",
]
