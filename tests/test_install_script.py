import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"


def run(script, cwd, env):
    return subprocess.run(["sh", str(script)], cwd=cwd, env=env, text=True, capture_output=True)


def test_install_requires_repository_root(tmp_path):
    result = run(SCRIPT, tmp_path, os.environ.copy())
    assert result.returncode != 0
    assert "repository root" in result.stderr


def test_install_runs_expected_commands(tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    log = tmp_path / "commands"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("npm", "uv", "hlm"):
        command = bindir / name
        command.write_text(f'#!/bin/sh\nprintf "{name} %s\\n" "$*" >> "$LOG"\n')
        command.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{bindir}:{env['PATH']}", LOG=str(log))
    result = run(checkout / "scripts/install.sh", checkout, env)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "npm ci", "npm run build", "uv tool install .", "hlm setup"
    ]
