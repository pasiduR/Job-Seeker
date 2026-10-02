"""Strict Pydantic output contracts for every LLM-assisted step."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScorerOutput(StrictOutput):
    score: int = Field(ge=1, le=10)
    reasons: list[str]
    missing_skills: list[str]


class AddedSkill(StrictOutput):
    skill: str
    est_days: int = Field(ge=0)
    plan: str


class TailorOutput(StrictOutput):
    tex: str
    changes: list[str]
    added_skills: list[AddedSkill]


class LatexFixOutput(StrictOutput):
    tex: str


class AnswerSource(StrEnum):
    PROFILE = "profile"
    CV = "cv"
    DRAFTED = "drafted"
    UNKNOWN = "unknown"


class FormAnswer(StrictOutput):
    field_id: str
    value: str | bool | list[str] | None
    source: AnswerSource


class FormMapperOutput(StrictOutput):
    answers: list[FormAnswer]


class ExtractedListing(StrictOutput):
    title: str
    company: str
    url: str
    description: str
    location: str | None = None
    posted_date: date | None = None

    @field_validator("title", "company", "url", "description")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ListingExtractorOutput(StrictOutput):
    listings: list[ExtractedListing]
