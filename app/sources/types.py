"""Source-neutral records produced by job discovery integrations."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class JobListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    url: str = Field(min_length=1)
    description: str = Field(min_length=1)
    location: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    source_job_id: str | None = None
    source_name: str
