from pathlib import Path


def test_project_layout_matches_agent_instructions(project_root: Path) -> None:
    expected_directories = (
        "app/db",
        "app/queue",
        "app/triggers",
        "app/sources",
        "app/steps",
        "app/llm/prompts",
        "app/browser",
        "app/notify",
        "app/dashboard",
        "tests/fixtures",
        "tests/evals",
    )

    missing = [
        directory
        for directory in expected_directories
        if not (project_root / directory).is_dir()
    ]

    assert missing == []
