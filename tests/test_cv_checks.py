from pathlib import Path

import pytest

from app.llm.schemas import AddedSkill
from app.queue.state_machine import JobStatus
from app.steps.base_cv import CVVersion
from app.steps.cv_checks import (
    apply_skill_placement,
    find_invented_entities,
    find_unsafe_commands,
    limit_added_skills,
)
from app.steps.latex import latex_to_text
from app.steps.tailor import Tailor, TailorSettings
from tests.test_tailor import FixtureTailorClient, tailor_output


@pytest.fixture
def base_tex(project_root: Path) -> str:
    return (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")


def settings(placement: str = "currently_learning") -> TailorSettings:
    return TailorSettings(
        skip_threshold=9,
        max_skill_days=7,
        max_added_skills=3,
        placement=placement,  # type: ignore[arg-type]
    )


def run_case(project_root: Path, base_tex: str, case: str, placement: str = "currently_learning"):
    output = tailor_output(project_root, base_tex, case)
    return Tailor(llm=FixtureTailorClient([output]), model="fixture").run(
        job_id=3,
        job_description="Backend role",
        job_score=6,
        base_cv=CVVersion(id=1, tex=base_tex, pdf_path="base.pdf"),
        settings=settings(placement),
    )


def test_reordered_cv_passes_entity_diff(project_root: Path, base_tex: str) -> None:
    output = tailor_output(project_root, base_tex, "reordered")
    assert find_invented_entities(base_tex, output.tex) == []
    assert find_unsafe_commands(base_tex, output.tex) == []


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("invented_employer", "name: Google"),
        ("invented_metric", "number: 75"),
        ("changed_date", "date: jun 2018"),
        ("invented_degree", "degree: msc"),
        ("invented_project", "name: Fleet"),
    ],
)
def test_entity_diff_rejects_invented_facts(
    project_root: Path, base_tex: str, case: str, expected: str
) -> None:
    decision = run_case(project_root, base_tex, case)

    assert decision.status == JobStatus.FAILED
    assert decision.tex is None
    assert decision.error is not None
    assert expected in decision.error


def test_unsafe_latex_is_rejected(project_root: Path, base_tex: str) -> None:
    decision = run_case(project_root, base_tex, "shell_escape")

    assert decision.status == JobStatus.FAILED
    assert decision.error is not None
    assert "write18" in decision.error


def test_skill_limits_drop_extras_long_and_existing_skills(
    project_root: Path, base_tex: str
) -> None:
    output = tailor_output(project_root, base_tex, "too_many_skills")
    result = limit_added_skills(
        output.added_skills, base_tex=base_tex, max_days=7, max_count=3
    )

    assert [skill.skill for skill in result.kept] == ["Redis", "Terraform", "GraphQL"]
    assert all(skill.est_days <= 7 for skill in result.kept)
    dropped = " ".join(result.dropped)
    assert "Kubernetes: 20 days" in dropped
    assert "Kafka: exceeds limit of 3" in dropped
    assert "docker: already in base CV" in dropped


def test_tailor_enforces_limits_and_currently_learning_placement(
    project_root: Path, base_tex: str
) -> None:
    decision = run_case(project_root, base_tex, "too_many_skills")

    assert decision.status == JobStatus.TAILORED
    assert len(decision.added_skills) == 3
    assert decision.tex is not None
    text = latex_to_text(decision.tex)
    assert "Currently learning: Redis, Terraform, GraphQL" in text
    assert "Kubernetes" not in text
    skills_start = decision.tex.index("\\section{Skills}")
    assert decision.tex.index("Currently learning") > skills_start


def test_skills_section_placement_extends_skill_list(
    project_root: Path, base_tex: str
) -> None:
    decision = run_case(project_root, base_tex, "reordered", placement="skills_section")

    assert decision.status == JobStatus.TAILORED
    assert decision.tex is not None
    assert "GitHub Actions, Linux, Redis" in decision.tex
    assert "Currently learning" not in decision.tex


def test_placement_adds_section_when_cv_has_no_skills_section() -> None:
    tex = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"
    skill = AddedSkill(skill="C#", est_days=5, plan="Build a console app.")

    placed = apply_skill_placement(tex, [skill], "currently_learning")

    assert "\\section{Currently Learning}\nC\\#" in placed
    assert placed.rstrip().endswith("\\end{document}")


def test_placement_is_noop_without_skills(base_tex: str) -> None:
    assert apply_skill_placement(base_tex, [], "skills_section") == base_tex
