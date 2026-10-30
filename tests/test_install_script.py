import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"


def run(script, cwd, env):
    return subprocess.run(
        ["sh", str(script)], cwd=cwd, env=env, text=True, capture_output=True
    )


def test_install_requires_repository_root(tmp_path):
    result = run(SCRIPT, tmp_path, os.environ.copy())
    assert result.returncode != 0
    assert "repository root" in result.stderr


def test_install_runs_expected_commands(tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(
        ROOT, checkout, ignore=shutil.ignore_patterns(".git", "__pycache__")
    )
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
        "npm ci",
        "npm run build",
        "uv tool install --upgrade .",
        "hlm setup",
    ]


def run_readme_installer(cwd, bindir, extra_env=None):
    readme = (ROOT / "README.md").read_text()
    command = re.search(
        r"### Install the CLI.*?```sh\n(.*?)\n```", readme, re.S
    ).group(1)
    env = os.environ.copy()
    env["PATH"] = str(bindir)
    env.update(extra_env or {})
    return subprocess.run(
        ["/bin/sh", "-c", command], cwd=cwd, env=env, text=True, capture_output=True
    )


def test_readme_installer_preflights_missing_commands_and_versions(tmp_path):
    for missing, message in (
        ("python3", "Python 3.12 or newer"),
        ("git", "install git"),
        ("uv", "install uv"),
        ("node", "Node.js 18 or newer"),
        ("npm", "install npm"),
    ):
        bindir = tmp_path / missing
        bindir.mkdir()
        for name in ("python3", "git", "uv", "node", "npm"):
            if name != missing:
                command = bindir / name
                body = (
                    'if [ "$1" = "-c" ] && '
                    '[ "${SIM_PYTHON_VERSION:-3.12}" != "3.12" ]; '
                    'then exit 1; fi; exit 0'
                    if name == "python3"
                    else 'printf "git %s\\n" "$*" >> "$GIT_LOG"; exit 0'
                    if name == "git"
                    else (
                        'if [ "$1" = "-e" ]; then '
                        '[ "${SIM_NODE_VERSION%%.*}" -ge 18 ] || exit 1; fi; exit 0'
                    )
                    if name == "node"
                    else "exit 0"
                )
                command.write_text(f"#!/bin/sh\n{body}\n")
                command.chmod(0o755)
        git_log = tmp_path / f"{missing}-git-log"
        result = run_readme_installer(
            tmp_path, bindir, {"GIT_LOG": str(git_log), "SIM_NODE_VERSION": "20.0.0"}
        )
        assert result.returncode != 0
        assert message in result.stderr
        assert not (tmp_path / f"{missing}-git-log").exists()

    bindir = tmp_path / "old-node"
    bindir.mkdir()
    git_log = tmp_path / "old-node-git-log"
    for name, body in (
        (
            "python3",
            'if [ "$1" = "-c" ] && '
            '[ "${SIM_PYTHON_VERSION:-3.12}" != "3.12" ]; '
            'then echo "Python 3.12 or newer is required." >&2; exit 1; fi; exit 0',
        ),
        ("git", 'printf "%s\\n" "$*" >> "$GIT_LOG"; exit 0'),
        ("uv", "exit 0"),
        ("npm", "exit 0"),
        (
            "node",
            (
                'if [ "$1" = "-e" ] && [ "${SIM_NODE_VERSION%%.*}" -lt 18 ]; '
                'then echo "Error: Node.js 18 or newer is required." >&2; exit 1; fi'
            ),
        ),
    ):
        command = bindir / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    result = run_readme_installer(
        tmp_path, bindir, {"GIT_LOG": str(git_log), "SIM_NODE_VERSION": "17.9.0"}
    )
    assert result.returncode != 0
    assert "Node.js 18 or newer is required" in result.stderr
    assert not git_log.exists()

    result = run_readme_installer(
        tmp_path,
        bindir,
        {
            "GIT_LOG": str(git_log),
            "SIM_NODE_VERSION": "20.0.0",
            "SIM_PYTHON_VERSION": "3.11",
        },
    )
    assert result.returncode != 0
    assert "Python 3.12 or newer is required" in result.stderr
    assert not git_log.exists()


def test_readme_installer_refuses_existing_checkout(tmp_path):
    (tmp_path / "harbor-ledger-memory").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("git", "echo unexpected >&2; exit 1"),
        ("uv", "exit 0"),
        ("npm", "exit 0"),
        ("node", "exit 0"),
        ("python3", "exit 0"),
    ):
        command = bindir / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    result = run_readme_installer(tmp_path, bindir)
    assert result.returncode != 0
    assert "already exists" in result.stderr
