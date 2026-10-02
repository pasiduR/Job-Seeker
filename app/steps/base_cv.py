"""Base CV storage with a single cached PDF compile per LaTeX revision."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


DEFAULT_CV_DIR = Path("storage/cvs")


@dataclass(frozen=True)
class CVVersion:
    id: int
    tex: str
    pdf_path: str


class PdfCompiler(Protocol):
    def compile(self, tex: str) -> bytes: ...


class BaseCVStore(Protocol):
    def get_base(self) -> CVVersion | None: ...

    def save_base(self, tex: str, pdf_path: str) -> CVVersion: ...


class CVConnection(Protocol):
    def execute(
        self, query: str, params: tuple[object, ...] | None = None
    ) -> Iterable[tuple[Any, ...]]: ...

    def transaction(self) -> AbstractContextManager[object]: ...


def _cv_version(row: tuple[Any, ...]) -> CVVersion:
    return CVVersion(id=int(row[0]), tex=str(row[1]), pdf_path=str(row[2]))


class PostgresBaseCVStore:
    def __init__(self, connection: CVConnection) -> None:
        self._connection = connection

    def get_base(self) -> CVVersion | None:
        with self._connection.transaction():
            rows = self._connection.execute(
                "SELECT id, tex, pdf_path FROM cv_versions WHERE is_base"
            )
            row = next(iter(rows), None)
        return _cv_version(row) if row is not None else None

    def save_base(self, tex: str, pdf_path: str) -> CVVersion:
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                INSERT INTO cv_versions (is_base, tex, pdf_path)
                VALUES (TRUE, %s, %s)
                ON CONFLICT (is_base) WHERE is_base
                DO UPDATE SET tex = EXCLUDED.tex, pdf_path = EXCLUDED.pdf_path
                RETURNING id, tex, pdf_path
                """,
                (tex, pdf_path),
            )
            row = next(iter(rows))
        return _cv_version(row)


def write_pdf(path: Path, pdf: bytes) -> None:
    """Write atomically so a crash never leaves a truncated CV behind."""

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(pdf)
    os.replace(partial, path)


class BaseCVService:
    def __init__(
        self,
        *,
        store: BaseCVStore,
        compiler: PdfCompiler,
        storage_dir: Path = DEFAULT_CV_DIR,
    ) -> None:
        self._store = store
        self._compiler = compiler
        self._storage_dir = storage_dir

    def save(self, tex: str) -> CVVersion:
        """Store the base CV, compiling only when the LaTeX or its PDF changed."""

        if not tex.strip():
            raise ValueError("base CV LaTeX must not be empty")
        current = self._store.get_base()
        if current is not None and current.tex == tex and _exists(current.pdf_path):
            return current
        pdf_path = self._compile(tex)
        return self._store.save_base(tex, str(pdf_path))

    def ensure_compiled(self) -> CVVersion:
        current = self._store.get_base()
        if current is None:
            raise LookupError("No base CV has been saved")
        if _exists(current.pdf_path):
            return current
        pdf_path = self._compile(current.tex)
        return self._store.save_base(current.tex, str(pdf_path))

    def _compile(self, tex: str) -> Path:
        digest = hashlib.sha256(tex.encode("utf-8")).hexdigest()[:16]
        path = self._storage_dir / f"base-{digest}.pdf"
        if not path.is_file():
            write_pdf(path, self._compiler.compile(tex))
        return path


def _exists(pdf_path: str) -> bool:
    return Path(pdf_path).is_file()
