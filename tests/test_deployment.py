import configparser
import json
from pathlib import Path


def test_service_templates_share_runtime_and_protect_secrets(project_root: Path):
    commands = json.loads((project_root / "tests/fixtures/systemd_commands.json").read_text())
    for service, module in commands.items():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(project_root / f"deploy/systemd/job-seeker-{service}.service")
        values = parser["Service"]
        assert values["User"] == "job-seeker"
        assert values["WorkingDirectory"] == "/var/lib/job-seeker"
        assert values["EnvironmentFile"] == "/etc/job-seeker/.env"
        assert values["ExecStart"] == f"/opt/job-seeker/.venv/bin/python -m {module}"
        assert values["UMask"] == "0077"
        assert values["NoNewPrivileges"] == "true"


def test_timers_are_only_wakeups_and_do_not_catch_up(project_root: Path):
    for name in ("scheduler", "watcher"):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(project_root / f"deploy/systemd/job-seeker-{name}.timer")
        assert parser["Timer"]["Unit"] == f"job-seeker-{name}.service"
        assert parser["Timer"]["Persistent"] == "false"
