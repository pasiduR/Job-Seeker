from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.dashboard.app import DashboardCredentials, create_app, get_repos


AUTH = ("admin", "fixture-password")
ORIGIN = {"origin": "http://testserver"}


def make_client(repos: Any = None) -> TestClient:
    router = APIRouter()

    @router.post("/echo")
    def echo(bundle: Any = Depends(get_repos)) -> dict[str, Any]:
        return {"repos": bundle}

    @contextmanager
    def factory() -> Iterator[Any]:
        yield repos

    app = create_app(
        repo_factory=factory,
        credentials=DashboardCredentials(
            username=AUTH[0], password=SecretStr(AUTH[1])
        ),
        routers=(router,),
    )
    return TestClient(app)


def test_health_check_is_public() -> None:
    response = make_client().get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pages_require_basic_auth() -> None:
    client = make_client()

    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    response = client.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Signed in as admin" in response.text
    assert response.headers["x-frame-options"] == "DENY"


def test_routers_get_auth_and_per_request_repos() -> None:
    client = make_client(repos="fixture-repos")

    assert client.post("/echo", headers=ORIGIN).status_code == 401
    response = client.post("/echo", auth=AUTH, headers=ORIGIN)
    assert response.json() == {"repos": "fixture-repos"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"origin": "https://evil.example"}, {"referer": "https://evil.example/x"}],
)
def test_cross_origin_posts_are_rejected(headers: dict[str, str]) -> None:
    response = make_client().post("/echo", auth=AUTH, headers=headers)

    assert response.status_code == 403


def test_credentials_must_be_configured() -> None:
    with pytest.raises(ValueError):
        DashboardCredentials(username="admin", password=SecretStr(""))


def test_static_stylesheet_is_served() -> None:
    response = make_client().get("/static/style.css")

    assert response.status_code == 200
    assert "--accent" in response.text
