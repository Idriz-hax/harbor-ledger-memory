"""Create ``harbor-ledger-memory-v<version>.zip`` with the Web UI included.

The first argument is the release tag and must be exactly ``v<version>``.
The output path must use the corresponding Harbor Ledger Memory basename.
"""

from __future__ import annotations

import argparse
import subprocess
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="release tag, exactly v<version>")
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--ref",
        help="git ref to archive (defaults to tag; useful for local verification)",
    )
    args = parser.parse_args()

    if (
        not args.tag.startswith("v")
        or len(args.tag) == 1
        or "/" in args.tag[1:]
        or "\\" in args.tag[1:]
    ):
        parser.error("tag must be exactly v<version>")
    version = args.tag[1:]
    expected_name = f"harbor-ledger-memory-v{version}.zip"
    if args.output.name != expected_name:
        parser.error(f"output must be named {expected_name}")
    prefix = f"harbor-ledger-memory-v{version}/"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            f"--output={args.output}",
            args.ref or args.tag,
        ],
        check=True,
    )

    web_dist = Path("web/dist")
    if not (web_dist / "index.html").is_file():
        raise FileNotFoundError(f"built Web UI not found: {web_dist / 'index.html'}")
    with zipfile.ZipFile(args.output, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in web_dist.rglob("*"):
            if path.is_file():
                archive.write(path, f"{prefix}{path.as_posix()}")


if __name__ == "__main__":
    main()
