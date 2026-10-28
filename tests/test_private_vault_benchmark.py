import json
from pathlib import Path

import pytest
from benchmarks.retrieval.private_vault import _relative_path, load_cases, load_scope


def test_scope_validates_explicit_rules_without_touching_paths(tmp_path: Path) -> None:
    scope = {
        "vault_path": "/not/read",
        "index_root": "/also/not/read",
        "folder_rules": {"AI": "read", "AI/token-optimizer": "deny"},
    }
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(scope))
    assert load_scope(path) == scope


def test_relative_paths_are_returned_canonically() -> None:
    assert _relative_path("folder/note.md", "expected path") == "folder/note.md"
    with pytest.raises(ValueError):
        _relative_path("../note.md", "expected path")


def test_example_scope_uses_generic_placeholders() -> None:
    example = (
        Path(__file__).parents[1] / "benchmarks/retrieval/local/scope.example.json"
    )
    scope = json.loads(example.read_text())
    assert scope["folder_rules"] == {
        "<readable-folder>": "read",
        "<denied-folder>": "deny",
    }


@pytest.mark.parametrize("rules", [{}, {"AI": "auto-write"}, {"../private": "read"}])
def test_scope_rejects_invalid_rules(tmp_path: Path, rules: dict) -> None:
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {"vault_path": "/vault", "index_root": "/index", "folder_rules": rules}
        )
    )
    with pytest.raises(ValueError):
        load_scope(path)


@pytest.mark.parametrize(
    "value",
    [
        {"id": "a", "query": "q", "expected_paths": ["/abs.md"]},
        {"id": "a", "query": "q", "expected_paths": ["../escape.md"]},
        {"id": "a", "query": "q", "expected_paths": ["AI//x.md"]},
        {"id": "a", "query": "q", "expected_paths": ["AI/x.md"], "unexpected": True},
        {"id": "a", "query": "q", "expected_paths": [], "expect_empty": False},
        {"id": "a", "query": "q", "expected_paths": []},
    ],
)
def test_cases_reject_invalid_entries(tmp_path: Path, value: dict) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(value) + "\n")
    with pytest.raises(ValueError):
        load_cases(path)


def test_cases_validate_duplicates_and_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    case = {"id": "a", "query": "q", "expected_paths": ["AI/x.md"]}
    path.write_text(json.dumps(case) + "\n" + json.dumps(case))
    with pytest.raises(ValueError):
        load_cases(path)
    path.write_text("{")
    with pytest.raises(ValueError):
        load_cases(path)


def test_private_local_ignore_allows_only_placeholder_and_gitignore(
    tmp_path: Path,
) -> None:
    import subprocess

    local = Path(__file__).parents[1] / "benchmarks/retrieval/local"
    allowed = subprocess.run(
        ["git", "check-ignore", "-q", str(local / "scope.example.json")]
    ).returncode
    assert allowed != 0
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(local / "cases.jsonl")]
    ).returncode
    assert ignored == 0
    assert (local / ".gitignore").read_text().splitlines() == [
        "*",
        "!.gitignore",
        "!scope.example.json",
    ]
