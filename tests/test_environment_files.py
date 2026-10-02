from pathlib import Path


def test_env_example_documents_required_secret_keys(project_root: Path) -> None:
    env_lines = (project_root / ".env.example").read_text(encoding="utf-8").splitlines()
    documented_keys = {
        line.partition("=")[0]
        for line in env_lines
        if line and not line.startswith("#") and "=" in line
    }
    required_keys = set(
        (project_root / "tests/fixtures/env_example_required_keys.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert documented_keys == required_keys


def test_gitignore_protects_secrets_and_runtime_artifacts(project_root: Path) -> None:
    ignore_rules = set(
        (project_root / ".gitignore").read_text(encoding="utf-8").splitlines()
    )

    assert {
        ".env",
        "/data/cvs/",
        "/data/screenshots/",
        "/data/browser-profiles/",
    } <= ignore_rules
