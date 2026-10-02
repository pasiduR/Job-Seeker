"""Application configuration from environment secrets and database settings."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsTableConnection(Protocol):
    """Small connection contract needed to read the settings table."""

    def execute(self, query: str) -> Iterable[tuple[str, object]]: ...


class SecretSettings(BaseSettings):
    """Credentials that must only come from the process environment or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    anthropic_workspace_id: str | None = None
    anthropic_workspace_header: str = "anthropic-workspace-id"
    gmail_client_id: SecretStr | None = None
    gmail_client_secret: SecretStr | None = None
    imap_host: str | None = None
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    ntfy_url: str | None = None
    ntfy_token: SecretStr | None = None
    dashboard_username: str | None = None
    dashboard_password: SecretStr | None = None


class RuntimeSettings(BaseModel):
    """Typed operational settings stored in the database."""

    model_config = ConfigDict(extra="allow", frozen=True)

    score_threshold: int = Field(default=7, ge=1, le=10)
    # Jobs scoring at or above this already fit the base CV, so tailoring is skipped.
    tailor_skip_threshold: int = Field(default=9, ge=1, le=10)
    max_skill_days: int = Field(default=7, ge=0)
    max_added_skills: int = Field(default=3, ge=0)
    skill_placement: Literal["skills_section", "currently_learning"] = (
        "currently_learning"
    )
    auto_submit: bool = False
    batch_daily_cap: int = Field(default=10, ge=0)
    jobspy_results_wanted: int = Field(default=20, ge=1)
    source_finder_types: list[
        Literal["job_board", "ats_board", "career_page", "rss", "email_alert"]
    ] = Field(default_factory=lambda: ["job_board"])
    fast_lane_daily_cap: int = Field(default=5, ge=0)
    llm_model: str = Field(default="claude-sonnet-4-6", min_length=1)
    # USD per million tokens, used for llm_calls cost logging.
    llm_input_usd_per_mtok: float = Field(default=3.0, ge=0)
    llm_output_usd_per_mtok: float = Field(default=15.0, ge=0)
    llm_max_tokens: int = Field(default=16000, ge=1)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    # Headed by default so CAPTCHAs and logins can be handled by hand.
    browser_headless: bool = False
    browser_timeout_seconds: float = Field(default=30.0, gt=0)
    # Public base URL of the dashboard, used for links in push notifications.
    dashboard_url: str = ""
    form_max_steps: int = Field(default=25, ge=1)
    form_max_pages: int = Field(default=6, ge=1)


class AppConfig(BaseModel):
    """Complete application configuration with secrets kept separate."""

    model_config = ConfigDict(frozen=True)

    secrets: SecretSettings
    settings: RuntimeSettings


def _decode_setting(value: object) -> object:
    if not isinstance(value, str):
        return value

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_settings_table(connection: SettingsTableConnection) -> dict[str, object]:
    """Read key/value settings without giving configuration code broader DB access."""

    rows = connection.execute("SELECT key, value FROM settings")
    return {key: _decode_setting(value) for key, value in rows}


def load_config(
    connection: SettingsTableConnection | None = None,
    *,
    env_file: str | Path = ".env",
) -> AppConfig:
    """Load secrets from ``env_file`` and operational values from ``settings``."""

    secrets = SecretSettings(_env_file=env_file)
    stored_values: dict[str, Any] = (
        read_settings_table(connection) if connection is not None else {}
    )
    runtime_settings = RuntimeSettings.model_validate(stored_values)
    return AppConfig(secrets=secrets, settings=runtime_settings)
