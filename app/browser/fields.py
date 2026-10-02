"""Extract fillable form fields (label, type, options, required) from the DOM."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


EXTRACT_SCRIPT = (Path(__file__).parent / "extract_fields.js").read_text(encoding="utf-8")
MAX_LABEL_LENGTH = 300
MAX_OPTIONS = 200

FieldType = Literal[
    "text",
    "email",
    "tel",
    "url",
    "number",
    "date",
    "password",
    "search",
    "textarea",
    "select",
    "combobox",
    "file",
    "checkbox",
    "radio_group",
    "checkbox_group",
    "other",
]
_KNOWN_TYPES = set(FieldType.__args__)  # type: ignore[attr-defined]


class FormField(BaseModel):
    """One question on the page. ``selector`` is only valid in ``frame`` of this page."""

    model_config = ConfigDict(frozen=True)

    field_id: str
    label: str
    type: FieldType
    required: bool
    options: list[str] = Field(default_factory=list)
    multiple: bool = False
    placeholder: str = ""
    accept: str = ""
    selector: str
    frame: int = 0

    @field_validator("label", "placeholder", "accept")
    @classmethod
    def _truncate(cls, value: str) -> str:
        return value[:MAX_LABEL_LENGTH]

    @field_validator("options")
    @classmethod
    def _limit_options(cls, value: list[str]) -> list[str]:
        return [option[:MAX_LABEL_LENGTH] for option in value[:MAX_OPTIONS]]

    def for_llm(self) -> dict[str, Any]:
        """The parts the form mapper needs; selectors and frames stay in code."""

        return self.model_dump(include={"field_id", "label", "type", "required", "options"})


class EvaluatingFrame(Protocol):
    def evaluate(self, expression: str, arg: Any = None) -> Any: ...


class FramedPage(Protocol):
    @property
    def frames(self) -> list[Any]: ...


def normalize(raw: dict[str, Any], frame: int) -> FormField:
    field_type = raw.get("type", "text")
    return FormField.model_validate(
        {
            **raw,
            "type": field_type if field_type in _KNOWN_TYPES else "other",
            "frame": frame,
        }
    )


def extract_fields(page: FramedPage) -> list[FormField]:
    """Fields from the main document and every iframe (embedded ATS forms)."""

    fields: list[FormField] = []
    for index, frame in enumerate(page.frames):
        try:
            raw_fields = frame.evaluate(EXTRACT_SCRIPT, f"f{index}_")
        except Exception:  # Detached or navigating frames have nothing to fill.
            continue
        fields.extend(normalize(raw, index) for raw in raw_fields)
    return fields
