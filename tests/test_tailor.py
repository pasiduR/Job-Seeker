import json
from pathlib import Path
from typing import Any

import pytest

from app.llm.schemas import TailorOutput
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion
from app.steps.tailor import PROMPT_VERSION, Tailor, TailorSettings


SETTINGS = TailorSettings(
    skip_threshold=9,
    max_skill_days=7,
    max_added_skills=3,
    placement="currently_learning",
)


class FixtureTailorClient:
    def __init__(self, outputs: list[TailorOutput]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.outputs.pop(0)


@pytest.fixture
def base_cv(project_root: Path) -> CVVersion:
    tex = (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")
    return CVVersion(id=1, tex=tex, pdf_path="storage/cvs/base.pdf")


@pytest.fixture
def job_description(project_root: Path) -> str:
    return (project_root / "tests/fixtures/job_backend_python.txt").read_text(
        encoding="utf-8"
    )


def tailor_output(project_root: Path, base_tex: str, case: str) -> TailorOutput:
    cases = json.loads(
        (project_root / "tests/fixtures/tailor_outputs.json").read_text(
            encoding="utf-8"
        )
    )
    spec = cases[case]
    tex = base_tex
    for old, new in spec["replacements"]:
        assert old in tex, old
        tex = tex.replace(old, new)
    return TailorOutput.model_validate(
        {"tex": tex, "changes": spec["changes"], "added_skills": spec["added_skills"]}
    )


def test_tailor_skips_llm_when_base_cv_already_fits(
    base_cv: CVVersion, job_description: str
) -> None:
    client = FixtureTailorClient([])
    decision = Tailor(llm=client, model="fixture").run(
        job_id=5,
        job_description=job_description,
        job_score=9,
        base_cv=base_cv,
        settings=SETTINGS,
    )

    assert decision.status == JobStatus.TAILORED
    assert decision.used_base is True
    assert client.calls == []


def test_tailor_calls_llm_with_versioned_prompt_and_limits(
    project_root: Path, base_cv: CVVersion, job_description: str
) -> None:
    output = tailor_output(project_root, base_cv.tex, "reordered")
    client = FixtureTailorClient([output])

    decision = Tailor(llm=client, model="fixture").run(
        job_id=5,
        job_description=job_description,
        job_score=7,
        base_cv=base_cv,
        settings=SETTINGS,
    )

    assert decision.used_base is False
    assert decision.status == JobStatus.TAILORED
    call = client.calls[0]
    assert call["prompt_version"] == PROMPT_VERSION
    assert call["step"] == "tailor"
    assert set(call["untrusted_data"]) == {"base_cv_tex", "job_description", "limits"}
    assert json.loads(call["untrusted_data"]["limits"]) == {
        "max_skill_days": 7,
        "max_added_skills": 3,
    }
