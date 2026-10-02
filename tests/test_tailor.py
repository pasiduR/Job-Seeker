import json
from pathlib import Path
from typing import Any

import pytest

from app.llm.schemas import LatexFixOutput, TailorOutput
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion
from app.steps.latex import LatexCompileError
from app.steps.tailor import FIX_PROMPT_VERSION, PROMPT_VERSION, Tailor, TailorSettings


SETTINGS = TailorSettings(
    skip_threshold=9,
    max_skill_days=7,
    max_added_skills=3,
    placement="currently_learning",
)


class FixtureCompiler:
    """Returns PDF bytes, or raises the queued compile errors in order."""

    def __init__(self, *results: bytes | Exception) -> None:
        self.results = list(results) or [b"%PDF-1.7 tailored"]
        self.compiled: list[str] = []

    def compile(self, tex: str) -> bytes:
        self.compiled.append(tex)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


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
    decision = Tailor(llm=client, compiler=FixtureCompiler(), model="fixture").run(
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

    decision = Tailor(llm=client, compiler=FixtureCompiler(), model="fixture").run(
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


def run_with_compiler(
    project_root: Path,
    base_cv: CVVersion,
    compiler: FixtureCompiler,
    fixes: list[str],
) -> tuple[Any, FixtureTailorClient]:
    output = tailor_output(project_root, base_cv.tex, "reordered")
    client = FixtureTailorClient([output, *(LatexFixOutput(tex=tex) for tex in fixes)])
    decision = Tailor(llm=client, compiler=compiler, model="fixture").run(
        job_id=5,
        job_description="Backend role",
        job_score=7,
        base_cv=base_cv,
        settings=SETTINGS,
    )
    return decision, client


def test_tailor_compiles_tailored_cv(project_root: Path, base_cv: CVVersion) -> None:
    compiler = FixtureCompiler()
    decision, client = run_with_compiler(project_root, base_cv, compiler, [])

    assert decision.status == JobStatus.TAILORED
    assert decision.pdf == b"%PDF-1.7 tailored"
    assert compiler.compiled == [decision.tex]
    assert len(client.calls) == 1


def test_tailor_makes_one_llm_fix_attempt(project_root: Path, base_cv: CVVersion) -> None:
    compiler = FixtureCompiler(
        LatexCompileError("pdflatex exited with status 1", "! Missing } inserted."),
        b"%PDF-1.7 fixed",
    )
    fixed_tex = base_cv.tex.replace("Linux", "Linux, Redis")

    decision, client = run_with_compiler(project_root, base_cv, compiler, [fixed_tex])

    assert decision.status == JobStatus.TAILORED
    assert decision.pdf == b"%PDF-1.7 fixed"
    assert decision.tex == fixed_tex
    fix_call = client.calls[1]
    assert fix_call["step"] == "tailor_fix"
    assert fix_call["prompt_version"] == FIX_PROMPT_VERSION
    assert fix_call["schema"] is LatexFixOutput
    assert "Missing }" in fix_call["untrusted_data"]["compile_log"]


def test_tailor_fails_after_second_compile_error(
    project_root: Path, base_cv: CVVersion
) -> None:
    compiler = FixtureCompiler(LatexCompileError("pdflatex exited with status 1", "! bad"))

    decision, client = run_with_compiler(project_root, base_cv, compiler, [base_cv.tex])

    assert decision.status == JobStatus.FAILED
    assert decision.pdf is None
    assert decision.error is not None
    assert "after one fix" in decision.error
    assert len(compiler.compiled) == 2
    assert len(client.calls) == 2


def test_tailor_rejects_fix_that_invents_content(
    project_root: Path, base_cv: CVVersion
) -> None:
    compiler = FixtureCompiler(LatexCompileError("failed", "! bad"))
    invented = base_cv.tex.replace("Contoso Health", "Initech")

    decision, _ = run_with_compiler(project_root, base_cv, compiler, [invented])

    assert decision.status == JobStatus.FAILED
    assert decision.error is not None
    assert "Initech" in decision.error
    assert len(compiler.compiled) == 1
