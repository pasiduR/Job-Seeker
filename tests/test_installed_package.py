import subprocess
import sys
import zipfile
from pathlib import Path


def test_installed_wheel_contains_fixture_required_schema_and_runtime_assets(project_root: Path, tmp_path: Path):
    # Build with local tooling only; never resolve/download dependencies.
    result = subprocess.run(
        [sys.executable, "-c", "import setuptools.build_meta as b; import sys; b.build_wheel(sys.argv[1])", str(tmp_path)],
        cwd=project_root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for migration in (project_root / "app/db/migrations").glob("*.sql"):
            assert f"app/db/migrations/{migration.name}" in names
        tables = (project_root / "tests/fixtures/required_tables.txt").read_text().splitlines()
        sql = archive.read("app/db/migrations/0001_initial.sql").decode()
        assert all(f"CREATE TABLE {table} (" in sql for table in tables)
        assert "app/dashboard/templates/schedules.html" in names
        assert "app/browser/extract_fields.js" in names
        assert "app/llm/prompts/form_map_v1.md" in names
        metadata = archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode()
        assert "Requires-Dist: tzdata" in metadata
