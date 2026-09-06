"""Regression coverage for shipping every Alembic migration in wheels."""

import subprocess
from pathlib import Path
from zipfile import ZipFile


def test_wheel_contains_every_source_migration(tmp_path: Path) -> None:
    """A built wheel must include each migration present in the source tree."""
    project_root = Path(__file__).resolve().parents[1]
    source_migrations = project_root / "backend" / "migrations" / "versions"
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=project_root,
        check=True,
    )

    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        packaged_migrations = {
            Path(name).name
            for name in wheel.namelist()
            if name.startswith("harbor_ledger_memory/migrations/versions/")
            and name.endswith(".py")
        }

    source_revision_files = {
        migration.name
        for migration in source_migrations.glob("*.py")
        if migration.name != "__init__.py"
    }
    assert source_revision_files <= packaged_migrations
