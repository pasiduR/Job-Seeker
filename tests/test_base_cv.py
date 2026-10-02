import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.steps.base_cv import BaseCVService, CVVersion
from app.steps.latex import (
    LatexCompileError,
    LatexCompiler,
    LatexEngineMissing,
    latex_to_text,
)


class CountingCompiler:
    def __init__(self) -> None:
        self.calls = 0

    def compile(self, tex: str) -> bytes:
        self.calls += 1
        return b"%PDF-1.7 fixture"


class MemoryBaseCVStore:
    def __init__(self) -> None:
        self.base: CVVersion | None = None
        self.saves = 0

    def get_base(self) -> CVVersion | None:
        return self.base

    def save_base(self, tex: str, pdf_path: str) -> CVVersion:
        self.saves += 1
        self.base = CVVersion(id=1, tex=tex, pdf_path=pdf_path)
        return self.base


@pytest.fixture
def base_tex(project_root: Path) -> str:
    return (project_root / "tests/fixtures/base_cv.tex").read_text(encoding="utf-8")


def test_base_cv_compiles_once_per_revision(base_tex: str, tmp_path: Path) -> None:
    store = MemoryBaseCVStore()
    compiler = CountingCompiler()
    service = BaseCVService(store=store, compiler=compiler, storage_dir=tmp_path)

    first = service.save(base_tex)
    second = service.save(base_tex)

    assert compiler.calls == 1
    assert first == second
    assert Path(first.pdf_path).read_bytes().startswith(b"%PDF")

    edited = service.save(base_tex.replace("Linux", "Linux, Bash"))
    assert compiler.calls == 2
    assert edited.pdf_path != first.pdf_path


def test_ensure_compiled_rebuilds_missing_pdf(base_tex: str, tmp_path: Path) -> None:
    store = MemoryBaseCVStore()
    compiler = CountingCompiler()
    service = BaseCVService(store=store, compiler=compiler, storage_dir=tmp_path)
    saved = service.save(base_tex)
    Path(saved.pdf_path).unlink()

    rebuilt = service.ensure_compiled()

    assert compiler.calls == 2
    assert Path(rebuilt.pdf_path).is_file()


def test_ensure_compiled_requires_a_base_cv(tmp_path: Path) -> None:
    service = BaseCVService(
        store=MemoryBaseCVStore(), compiler=CountingCompiler(), storage_dir=tmp_path
    )
    with pytest.raises(LookupError):
        service.ensure_compiled()


def _fake_runner(returncode: int, write_pdf: bool):
    commands: list[list[str]] = []

    def run(
        command: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        assert (cwd / "cv.tex").is_file()
        if write_pdf:
            (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fixture")
        return subprocess.CompletedProcess(command, returncode, "out", "! Undefined")

    return run, commands


def test_compiler_prefers_tectonic_and_returns_pdf_bytes() -> None:
    runner, commands = _fake_runner(0, write_pdf=True)
    compiler = LatexCompiler(runner=runner, which=lambda name: f"/bin/{name}")

    assert compiler.compile("\\documentclass{article}") == b"%PDF-1.7 fixture"
    assert commands[0][0] == "tectonic"


def test_compiler_falls_back_to_pdflatex_without_shell_escape() -> None:
    runner, commands = _fake_runner(0, write_pdf=True)
    compiler = LatexCompiler(
        runner=runner,
        which=lambda name: "/bin/pdflatex" if name == "pdflatex" else None,
    )

    compiler.compile("x")

    assert commands[0][0] == "pdflatex"
    assert "-no-shell-escape" in commands[0]


def test_compiler_reports_document_errors_with_log() -> None:
    runner, _ = _fake_runner(1, write_pdf=False)
    compiler = LatexCompiler(runner=runner, which=lambda name: name)

    with pytest.raises(LatexCompileError) as excinfo:
        compiler.compile("x")
    assert "Undefined" in excinfo.value.log


def test_compiler_reports_timeouts_as_compile_errors() -> None:
    def runner(
        command: Sequence[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout)

    compiler = LatexCompiler(runner=runner, which=lambda name: name, timeout_seconds=1)
    with pytest.raises(LatexCompileError):
        compiler.compile("x")


def test_compiler_distinguishes_missing_engine() -> None:
    compiler = LatexCompiler(which=lambda name: None)
    with pytest.raises(LatexEngineMissing):
        compiler.compile("x")


def test_latex_to_text_keeps_visible_content(base_tex: str) -> None:
    text = latex_to_text(base_tex)

    assert "Northwind Logistics" in text
    assert "40%" in text
    assert "Fixture CV" not in text
    assert "documentclass" not in text
    assert "\\" not in text
