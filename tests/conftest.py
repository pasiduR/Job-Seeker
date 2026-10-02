from pathlib import Path
import tomllib
from typing import Any

import pytest


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def pyproject_data(project_root: Path) -> dict[str, Any]:
    with (project_root / "pyproject.toml").open("rb") as pyproject_file:
        return tomllib.load(pyproject_file)
