"""Map extracted form fields to answers, then enforce the no-guessing rules in code."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.browser.fields import FormField
from app.llm.schemas import AnswerSource, FormAnswer, FormMapperOutput


PROMPT_VERSION = "form_map_v1"
PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"
# File inputs never go to the LLM; the filler uploads the tailored CV PDF here.
RESUME_UPLOAD = "@tailored_cv_pdf"
MAX_DRAFT_LENGTH = 4000

Value = str | bool | list[str] | None


class SensitiveCategory(StrEnum):
    WORK_AUTHORIZATION = "work_authorization"
    SALARY = "salary"
    DEMOGRAPHIC = "demographic"
    LEGAL = "legal"


# Matched against field labels, and against profile key paths to find the
# profile fields allowed to answer that category.
_SENSITIVE_PATTERNS = {
    SensitiveCategory.WORK_AUTHORIZATION: re.compile(
        r"authori[sz]|visa|sponsor|right[ _-]?to[ _-]?work|work[ _-]?permit|citizen"
        r"|immigration|eligib",
        re.IGNORECASE,
    ),
    SensitiveCategory.SALARY: re.compile(
        r"salary|compensation|\bpay\b|pay[ _-]|remuneration|\bctc\b|hourly[ _-]?rate"
        r"|day[ _-]?rate|expected[ _-]?rate",
        re.IGNORECASE,
    ),
    SensitiveCategory.DEMOGRAPHIC: re.compile(
        r"gender|\bsex\b|race|ethnic|veteran|disab|pronoun|orientation|\bage\b"
        r"|birth|religio|hispanic|latin[oax]|transgender",
        re.IGNORECASE,
    ),
    SensitiveCategory.LEGAL: re.compile(
        r"criminal|convict|felony|background[ _-]?check|non[ _-]?compete|lawsuit"
        r"|consent|\bagree|acknowledg|certify|attest|terms|privacy",
        re.IGNORECASE,
    ),
}
_RESUME_LABEL = re.compile(r"resume|résumé|\bcv\b|curriculum", re.IGNORECASE)
_CHOICE_TYPES = {"select", "radio_group", "checkbox_group", "checkbox"}
_YES = {"yes", "true", "y", "i agree", "agree"}
_NO = {"no", "false", "n"}


class FormMapperClient(Protocol):
    def generate(
        self,
        *,
        schema: type[FormMapperOutput],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> FormMapperOutput: ...


@dataclass(frozen=True)
class FieldAnswer:
    field: FormField
    value: Value
    source: AnswerSource
    flag: str | None = None

    @property
    def is_unknown(self) -> bool:
        return self.source == AnswerSource.UNKNOWN


@dataclass(frozen=True)
class MappedForm:
    answers: tuple[FieldAnswer, ...]

    @property
    def needs_manual_reasons(self) -> list[str]:
        """Required fields the code could not answer; any one means ``needs_manual``."""

        return [
            f"required field unanswered: {answer.field.label or answer.field.field_id}"
            for answer in self.answers
            if answer.field.required and answer.is_unknown
        ]

    @property
    def flagged(self) -> list[FieldAnswer]:
        """Answers a human should look at during review."""

        return [
            answer
            for answer in self.answers
            if answer.flag or answer.source in {AnswerSource.DRAFTED, AnswerSource.UNKNOWN}
        ]


def sensitive_category(label: str) -> SensitiveCategory | None:
    for category, pattern in _SENSITIVE_PATTERNS.items():
        if pattern.search(label):
            return category
    return None


def profile_leaves(profile: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Every scalar in the profile with its dotted key path."""

    def walk(node: Any, path: str) -> Iterator[tuple[str, Any]]:
        if isinstance(node, Mapping):
            for key, child in node.items():
                yield from walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for child in node:
                yield from walk(child, path)
        elif node is not None:
            yield path, node

    return list(walk(profile, ""))


class FormMapper:
    def __init__(self, *, llm: FormMapperClient, model: str) -> None:
        self._llm = llm
        self._model = model

    def run(
        self,
        *,
        job_id: int,
        fields: Sequence[FormField],
        profile: Mapping[str, Any],
        cv_text: str,
    ) -> MappedForm:
        asked = [field for field in fields if field.type != "file"]
        output = FormMapperOutput(answers=[])
        if asked:
            output = self._llm.generate(
                schema=FormMapperOutput,
                job_id=job_id,
                step="form_mapper",
                prompt_version=PROMPT_VERSION,
                model=self._model,
                prompt=PROMPT_PATH.read_text(encoding="utf-8"),
                untrusted_data={
                    # One field per line: if the sanitizer drops a hostile label,
                    # only that field is lost and it is then treated as unknown.
                    "form_fields": "\n".join(
                        json.dumps(field.for_llm(), ensure_ascii=False) for field in asked
                    ),
                    "profile": json.dumps(profile, ensure_ascii=False, indent=2),
                    "tailored_cv": cv_text,
                },
            )
        return enforce_rules(fields, output, profile)


def enforce_rules(
    fields: Sequence[FormField],
    output: FormMapperOutput,
    profile: Mapping[str, Any],
) -> MappedForm:
    """Code-side guarantees, whatever the LLM returned."""

    by_id: dict[str, FormAnswer] = {}
    for answer in output.answers:
        by_id.setdefault(answer.field_id, answer)  # unknown ids are simply ignored
    leaves = profile_leaves(profile)
    return MappedForm(
        tuple(_check(field, by_id.get(field.field_id), leaves) for field in fields)
    )


def _unknown(field: FormField, flag: str | None = None) -> FieldAnswer:
    return FieldAnswer(field=field, value=None, source=AnswerSource.UNKNOWN, flag=flag)


def _check(
    field: FormField, answer: FormAnswer | None, leaves: list[tuple[str, Any]]
) -> FieldAnswer:
    if field.type == "file":
        if _RESUME_LABEL.search(field.label):
            return FieldAnswer(field=field, value=RESUME_UPLOAD, source=AnswerSource.CV)
        return _unknown(field, "file upload other than the CV")
    if answer is None:
        return _unknown(field, "no answer returned")
    if answer.source == AnswerSource.UNKNOWN:
        return _unknown(field)

    value = _coerce(field, answer.value)
    if value is None:
        return _unknown(field, f"invalid value for a {field.type} field")

    category = sensitive_category(field.label)
    if category is not None:
        if answer.source != AnswerSource.PROFILE:
            return _unknown(field, f"{category.value} question not answered from profile")
        allowed = [
            (path, leaf)
            for path, leaf in leaves
            if _SENSITIVE_PATTERNS[category].search(path)
        ]
        if not _supported(value, allowed):
            return _unknown(field, f"{category.value} answer not found in profile")
        # Code can check the answer comes from a matching profile field, not
        # that a yes/no has the right polarity, so a human always confirms it.
        return FieldAnswer(
            field=field,
            value=value,
            source=answer.source,
            flag=f"{category.value} answer from profile; confirm before submitting",
        )

    if answer.source == AnswerSource.DRAFTED and field.type in _CHOICE_TYPES:
        return _unknown(field, "drafted answer for a choice field")
    if answer.source == AnswerSource.PROFILE:
        related = [
            (path, leaf) for path, leaf in leaves if not isinstance(leaf, bool)
        ] + [
            (path, leaf)
            for path, leaf in leaves
            if isinstance(leaf, bool) and _shares_word(path, field.label)
        ]
        if not _supported(value, related):
            return _unknown(field, "profile answer not found in profile")
    return FieldAnswer(field=field, value=value, source=answer.source)


def _coerce(field: FormField, value: Value) -> Value:
    """Normalize the value for the field type, or None when it does not fit."""

    if field.type == "checkbox":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _YES | _NO:
            return value.strip().lower() in _YES
        return None
    if field.type == "checkbox_group":
        items = value if isinstance(value, list) else None
        if items is None:
            return None
        chosen = [_match_option(field, item) for item in items]
        return None if any(item is None for item in chosen) else chosen  # type: ignore[return-value]
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if not isinstance(value, str) or not value.strip():
        return None
    if field.type in {"select", "radio_group"}:
        return _match_option(field, value)
    return value.strip()[:MAX_DRAFT_LENGTH]


def _match_option(field: FormField, value: str) -> str | None:
    wanted = _norm(value)
    for option in field.options:
        if _norm(option) == wanted:
            return option
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9@.+]+", " ", text.lower()).strip()


def _shares_word(path: str, label: str) -> bool:
    path_words = {word for word in re.split(r"[^a-z]+", path.lower()) if len(word) >= 4}
    label_words = {word for word in re.split(r"[^a-z]+", label.lower()) if len(word) >= 4}
    return bool(path_words & label_words)


def _supported(value: Value, leaves: list[tuple[str, Any]]) -> bool:
    values = value if isinstance(value, list) else [value]
    return all(any(_matches(item, leaf) for _, leaf in leaves) for item in values)


def _matches(value: str | bool | None, leaf: Any) -> bool:
    if value is None:
        return False
    if isinstance(leaf, bool):
        if isinstance(value, bool):
            return value == leaf
        word = _norm(value)
        return (word in _YES and leaf) or (word in _NO and not leaf)
    if isinstance(value, bool):
        return False
    have, want = _norm(str(leaf)), _norm(value)
    if not have or not want:
        return False
    return _contains_words(have, want) or _contains_words(want, have)


def _contains_words(part: str, whole: str) -> bool:
    return f" {part} " in f" {whole} "
