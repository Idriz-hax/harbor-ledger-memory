"""Hermes profile setup without touching profile secrets."""

import json
import os
import tempfile
from pathlib import Path


def setup(*, hermes_home: Path | None = None) -> Path:
    home = hermes_home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    home = home.expanduser().resolve()
    plugin = (home / "plugins" / "hlm").resolve()
    if not plugin.is_relative_to(home):
        raise ValueError("Hermes plugin path escapes selected profile")
    plugin.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[4] / "integrations" / "hermes" / "hlm"
    for name in ("__init__.py", "client.py"):
        src = source / name
        if not src.is_file():
            raise FileNotFoundError(f"Hermes integration resource missing: {src}")
        target = plugin / name
        fd, temp = tempfile.mkstemp(dir=plugin)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(src.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    config = plugin / "config.json"
    value = {}
    if config.exists():
        value = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{config} must contain a JSON object")
    value.setdefault("base_url", "http://127.0.0.1:8765")
    fd, temp = tempfile.mkstemp(dir=plugin)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, config)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return config
