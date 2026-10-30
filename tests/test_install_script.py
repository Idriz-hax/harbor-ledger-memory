import os
import re
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


def run_readme_installer(cwd, bindir):
    readme = (ROOT / "README.md").read_text()
    command = re.search(r"```sh\n(.*?)\n```", readme, re.S).group(1)
    env = os.environ.copy()
    env["PATH"] = str(bindir)
    return subprocess.run(["/bin/sh", "-c", command], cwd=cwd, env=env, text=True, capture_output=True)


def test_readme_installer_preflights_missing_commands_and_node_version(tmp_path):
    for missing, message in (("git", "install git"), ("uv", "install uv"),
                             ("node", "Node.js 18 or newer"), ("npm", "install npm")):
        bindir = tmp_path / missing
        bindir.mkdir()
        for name in ("git", "uv", "node", "npm"):
            if name != missing:
                command = bindir / name
                command.write_text('#!/bin/sh\n[ "$1" = "-e" ] && exit 0\nexit 0\n')
                command.chmod(0o755)
        result = run_readme_installer(tmp_path, bindir)
        assert result.returncode != 0
        assert message in result.stderr

    bindir = tmp_path / "old-node"
    bindir.mkdir()
    for name, body in (("git", "exit 0"), ("uv", "exit 0"), ("npm", "exit 0"),
                       ("node", 'echo "Error: Node.js 18 or newer is required." >&2; exit 1')):
        command = bindir / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    result = run_readme_installer(tmp_path, bindir)
    assert result.returncode != 0
    assert "Node.js 18 or newer is required" in result.stderr


def test_readme_installer_refuses_existing_checkout(tmp_path):
    (tmp_path / "harbor-ledger-memory").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("git", "echo unexpected >&2; exit 1"), ("uv", "exit 0"),
                       ("npm", "exit 0"), ("node", "exit 0")):
        command = bindir / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    result = run_readme_installer(tmp_path, bindir)
    assert result.returncode != 0
    assert "already exists" in result.stderr
