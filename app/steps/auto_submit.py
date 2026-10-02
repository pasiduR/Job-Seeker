"""Fail-closed policy for automatic approval. Submission remains separate."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import RuntimeSettings


class StoredAnswer(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    field_id: str
    source: Literal["profile", "cv", "drafted", "unknown"]
    required: bool
    value: str | bool | list[str] | None
    flag: str | None


class AutoSubmitFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int | None
    source_id: int | None
    answers: list[dict[str, Any]]
    added_skill_count: int = Field(ge=0)
    outcome: str


def can_auto_submit(facts: AutoSubmitFacts | None, settings: RuntimeSettings) -> bool:
    if not settings.auto_submit or facts is None or facts.outcome != "filled":
        return False
    if facts.score is None or facts.score < settings.auto_submit_score_threshold:
        return False
    if facts.source_id not in settings.trusted_source_ids or facts.added_skill_count:
        return False
    if not facts.answers:
        return False
    try:
        answers = [StoredAnswer.model_validate(answer) for answer in facts.answers]
    except ValidationError:
        return False
    return all(
        answer.flag is None and answer.source in {"profile", "cv"}
        and (not answer.required or answer.value not in (None, "", []))
        for answer in answers
    )
