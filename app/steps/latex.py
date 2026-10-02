"""Sandboxed LaTeX compilation and plain-text extraction for CV steps."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path


CommandRunner = Callable[[Sequence[str], Path, float], "subprocess.CompletedProcess[str]"]
Which = Callable[[str], str | None]

_SOURCE_NAME = "cv.tex"
_PDF_NAME = "cv.pdf"
_LOG_TAIL_CHARS = 4000


class LatexEngineMissing(RuntimeError):
    """No supported LaTeX engine is installed; not a document error."""


class LatexCompileError(RuntimeError):
    """The document failed to compile; ``log`` holds the engine output tail."""

    def __init__(self, message: str, log: str) -> None:
        super().__init__(message)
        self.log = log


def _run(
    command: Sequence[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _engine_command(engine: str) -> list[str]:
    if engine == "tectonic":
        return ["tectonic", "--chatter", "minimal", "--outdir", ".", _SOURCE_NAME]
    if engine == "pdflatex":
        return [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            _SOURCE_NAME,
        ]
    raise ValueError(f"Unsupported LaTeX engine: {engine!r}")


class LatexCompiler:
    def __init__(
        self,
        *,
        engine: str = "auto",
        timeout_seconds: float = 60.0,
        runner: CommandRunner = _run,
        which: Which = shutil.which,
    ) -> None:
        if engine not in {"auto", "tectonic", "pdflatex"}:
            raise ValueError(f"Unsupported LaTeX engine: {engine!r}")
        self._engine = engine
        self._timeout = timeout_seconds
        self._run = runner
        self._which = which

    def resolve_engine(self) -> str:
        candidates = (
            ("tectonic", "pdflatex") if self._engine == "auto" else (self._engine,)
        )
        for candidate in candidates:
            if self._which(candidate) is not None:
                return candidate
        raise LatexEngineMissing(
            "No LaTeX engine found; install tectonic or pdflatex"
        )

    def compile(self, tex: str) -> bytes:
        engine = self.resolve_engine()
        with tempfile.TemporaryDirectory(prefix="cv-build-") as build_dir:
            workdir = Path(build_dir)
            (workdir / _SOURCE_NAME).write_text(tex, encoding="utf-8")
            try:
                result = self._run(_engine_command(engine), workdir, self._timeout)
            except subprocess.TimeoutExpired as exc:
                raise LatexCompileError(
                    f"{engine} timed out after {self._timeout:g}s", ""
                ) from exc
            log = _tail((result.stdout or "") + (result.stderr or ""))
            pdf_path = workdir / _PDF_NAME
            if result.returncode != 0 or not pdf_path.is_file():
                raise LatexCompileError(
                    f"{engine} exited with status {result.returncode}", log
                )
            return pdf_path.read_bytes()


def _tail(text: str) -> str:
    return text[-_LOG_TAIL_CHARS:]


_COMMENT = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_PREAMBLE = re.compile(r"\A.*?\\begin\{document\}", re.DOTALL)
_COMMAND = re.compile(r"\\[A-Za-z@]+\*?(\[[^\]]*\])?")
_ESCAPED = re.compile(r"\\([&%$#_{}])")


def latex_to_text(tex: str) -> str:
    """Approximate the visible text of a LaTeX document for LLM context and checks."""

    body = _COMMENT.sub("", tex)
    body = _PREAMBLE.sub("", body)
    body = body.replace("\\end{document}", "")
    body = _ESCAPED.sub(r"\1", body)
    body = body.replace("\\\\", "\n")
    body = re.sub(r"\\(begin|end)\{[^}]*\}", "\n", body)
    body = _COMMAND.sub(" ", body)
    body = body.replace("{", " ").replace("}", " ").replace("~", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines())
    return "\n".join(line for line in lines if line)
