"""Verify release archives contain the Web UI and serve it from the wheel."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def release_version(
    artifacts: list[Path], *, version: str | None, tag: str | None
) -> str:
    """Return the release version, from --version, --tag, or artifact names.

    With no option, all three required artifact basenames are parsed and must
    agree.  A tag must be exactly ``v<version>``; this is also the source ZIP
    directory prefix convention used by ``create_source_archive.py``.
    """
    if version is not None and tag is not None:
        raise ValueError("--version and --tag are mutually exclusive")
    if tag is not None:
        if not tag.startswith("v") or len(tag) == 1:
            raise ValueError("release tag must be v<version>")
        version = tag[1:]
    if version is not None:
        if not version or "/" in version or "\\" in version:
            raise ValueError("release version must be a non-empty path-free value")
        return version

    patterns = (
        re.compile(r"^harbor_ledger_memory-(.+)-py3-none-any\.whl$"),
        re.compile(r"^harbor_ledger_memory-(.+)\.tar\.gz$"),
        re.compile(r"^harbor-ledger-memory-v(.+)\.zip$"),
    )
    versions: set[str] = set()
    for pattern in patterns:
        matches = [
            match
            for artifact in artifacts
            if (match := pattern.match(artifact.name))
        ]
        if len(matches) != 1:
            raise ValueError(
                "without --version/--tag, exactly one wheel, sdist, and source ZIP "
                "with Harbor Ledger Memory names are required"
            )
        versions.add(matches[0].group(1))
    if len(versions) != 1:
        raise ValueError("release artifact versions do not agree")
    return versions.pop()


def members(path: Path) -> set[str]:
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path) as archive:
            return set(archive.getnames())
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def verify_web_files(path: Path) -> None:
    names = members(path)
    bundle = "harbor_ledger_memory/web_dist/" if path.suffix == ".whl" else "web/dist/"
    assert any(name.endswith(f"{bundle}index.html") for name in names), path
    assert any(f"{bundle}assets/" in name for name in names), path


def verify_sdist_contents(path: Path) -> None:
    """Keep source distributions free of the Web UI dependency tree."""
    if not path.name.endswith((".tar.gz", ".tgz")):
        return
    names = members(path)
    assert not any(
        "/web/node_modules/" in f"/{name.lstrip('/')}" or
        name.endswith("/web/node_modules")
        for name in names
    ), f"dependency tree included in source distribution: {path}"


def verify_identity(path: Path, version: str) -> None:
    expected = {
        ".whl": f"harbor_ledger_memory-{version}-py3-none-any.whl",
        ".gz": f"harbor_ledger_memory-{version}.tar.gz",
        ".zip": f"harbor-ledger-memory-v{version}.zip",
    }
    if path.name.endswith(".tar.gz"):
        expected_name = expected[".gz"]
    else:
        expected_name = expected[path.suffix]
    assert path.name == expected_name, (
        f"unexpected {path} name; expected {expected_name}"
    )
    if path.suffix == ".zip":
        prefix = f"harbor-ledger-memory-v{version}/"
        with zipfile.ZipFile(path) as archive:
            assert all(name.startswith(prefix) for name in archive.namelist()), path


def verify_app_serves_wheel(wheel: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        extracted = Path(directory)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(extracted)
        web_index = next(extracted.rglob("harbor_ledger_memory/web_dist/index.html"))
        code = """
from pathlib import Path
from fastapi.testclient import TestClient
from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import Settings

vault = Path.cwd() / "vault"
(vault / "AI").mkdir(parents=True)
response = TestClient(create_app(Settings(HLM_VAULT_PATH=vault))).get("/")
assert response.status_code == 200
assert response.text == Path(__import__("sys").argv[1]).read_text()
"""
        subprocess.run(
            [sys.executable, "-c", code, str(web_index)],
            cwd=extracted,
            env={**os.environ, "PYTHONPATH": str(extracted)},
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verify Harbor Ledger Memory artifacts. Version is taken from --version, "
            "--tag (v<version>), or matching required artifact basenames."
        )
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--version", help="release version without the leading v")
    group.add_argument("--tag", help="release tag, exactly v<version>")
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        version = release_version(args.artifacts, version=args.version, tag=args.tag)
    except ValueError as error:
        parser.error(str(error))
    for artifact in args.artifacts:
        verify_identity(artifact, version)
        verify_web_files(artifact)
        verify_sdist_contents(artifact)
    wheel = next(path for path in args.artifacts if path.suffix == ".whl")
    verify_app_serves_wheel(wheel)


if __name__ == "__main__":
    main()
