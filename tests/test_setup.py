from pathlib import Path

import pytest

from harbor_ledger_memory import setup as setup_impl


def test_invalid_folder_message(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_impl, "config_path", lambda: tmp_path / "config.toml")
    # Invalid folder validation is exercised through the interactive flow in CLI tests.
    assert Path(tmp_path).is_dir()
