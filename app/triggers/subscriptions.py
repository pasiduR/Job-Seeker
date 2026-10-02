"""Validated subscription input; polling is executed by the queue worker."""

from pydantic import BaseModel, ConfigDict, Field


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: int = Field(gt=0)
    search_filter_id: int = Field(gt=0)
    polling_interval_minutes: int = Field(default=10, gt=0)
