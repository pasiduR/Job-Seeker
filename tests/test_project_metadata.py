from typing import Any


def test_pyproject_declares_required_runtime(pyproject_data: dict[str, Any]) -> None:
    project = pyproject_data["project"]
    dependencies = project["dependencies"]

    assert project["requires-python"] == ">=3.11"
    for package in (
        "anthropic",
        "crawl4ai",
        "playwright",
        "psycopg",
        "pydantic",
        "pydantic-settings",
        "python-jobspy",
    ):
        assert any(dependency.startswith(package) for dependency in dependencies)


def test_pyproject_has_test_dependency(pyproject_data: dict[str, Any]) -> None:
    dev_dependencies = pyproject_data["project"]["optional-dependencies"]["dev"]

    assert any(dependency.startswith("pytest") for dependency in dev_dependencies)
