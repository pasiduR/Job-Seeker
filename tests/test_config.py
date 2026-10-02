import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.config import load_config


class FakeSettingsConnection:
    def __init__(self, rows: Iterable[tuple[str, object]]) -> None:
        self._rows = list(rows)
        self.executed_query: str | None = None

    def execute(self, query: str) -> Iterable[tuple[str, object]]:
        self.executed_query = query
        return self._rows


def test_load_config_uses_env_secrets_and_settings_rows(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("DATABASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    env_file = project_root / "tests/fixtures/config.env"
    fixture_rows = json.loads(
        (project_root / "tests/fixtures/settings_table_rows.json").read_text(
            encoding="utf-8"
        )
    )
    connection = FakeSettingsConnection(fixture_rows)

    config = load_config(connection, env_file=env_file)

    assert config.secrets.database_url is not None
    assert config.secrets.database_url.get_secret_value().endswith("/jobs")
    assert config.secrets.llm_api_key is not None
    assert config.secrets.llm_api_key.get_secret_value() == "test-key"
    assert config.settings.score_threshold == 8
    assert config.settings.max_skill_days == 5
    assert config.settings.max_added_skills == 2
    assert config.settings.skill_placement == "skills_section"
    assert config.settings.auto_submit is True
    assert config.settings.batch_daily_cap == 12
    assert config.settings.fast_lane_daily_cap == 4
    assert connection.executed_query == "SELECT key, value FROM settings"


def test_load_config_supplies_safe_runtime_defaults(project_root: Path) -> None:
    config = load_config(env_file=project_root / "tests/fixtures/missing.env")

    assert config.settings.score_threshold == 7
    assert config.settings.tailor_skip_threshold == 9
    assert config.settings.max_skill_days == 7
    assert config.settings.max_added_skills == 3
    assert config.settings.skill_placement == "currently_learning"
    assert config.settings.auto_submit is False
    assert config.settings.batch_daily_cap == 10
    assert config.settings.fast_lane_daily_cap == 5
