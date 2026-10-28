import json
from pathlib import Path

import pytest
from benchmarks.retrieval.private_vault import (
    _relative_path,
    load_cases,
    load_scope,
    run_private_benchmark,
)


def test_scope_validates_explicit_rules_without_touching_paths(tmp_path: Path) -> None:
    scope = {
        "vault_path": "/not/read",
        "index_root": ".",
        "folder_rules": {".": "read", "folder/denied": "deny"},
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
        ".": "read",
        "<denied-folder>": "deny",
    }


@pytest.mark.parametrize("rules", [{}, {"AI": "auto-write"}, {"../private": "read"}])
def test_scope_rejects_invalid_rules(tmp_path: Path, rules: dict) -> None:
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {"vault_path": "/vault", "index_root": "index", "folder_rules": rules}
        )
    )
    with pytest.raises(ValueError):
        load_scope(path)


@pytest.mark.parametrize(
    "index_root", ["/absolute/index", "../outside", "nested/../outside"]
)
def test_scope_rejects_unsafe_index_root(tmp_path: Path, index_root: str) -> None:
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {
                "vault_path": "/vault",
                "index_root": index_root,
                "folder_rules": {".": "read"},
            }
        )
    )
    with pytest.raises(ValueError):
        load_scope(path)


def test_scope_accepts_root_index_and_read_rule(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    scope = {
        "vault_path": "/not/read",
        "index_root": ".",
        "folder_rules": {".": "read"},
    }
    path.write_text(json.dumps(scope))
    assert load_scope(path) == scope


@pytest.mark.parametrize(
    ("index_root", "folder_rules"),
    [
        (".", {"denied-folder": "deny"}),
        ("AI", {".": "read"}),
        ("AI", {"AI": "deny"}),
        ("AI", {"AI": "read", "Other": "deny"}),
    ],
)
def test_scope_rejects_missing_or_outside_read_root(
    tmp_path: Path, index_root: str, folder_rules: dict
) -> None:
    path = tmp_path / "scope.json"
    path.write_text(
        json.dumps(
            {
                "vault_path": "/not/read",
                "index_root": index_root,
                "folder_rules": folder_rules,
            }
        )
    )
    with pytest.raises(ValueError):
        load_scope(path)


def test_scope_accepts_explicit_read_root_with_nested_deny(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    scope = {
        "vault_path": "/not/read",
        "index_root": "AI",
        "folder_rules": {"AI": "read", "AI/hidden": "deny"},
    }
    path.write_text(json.dumps(scope))
    assert load_scope(path) == scope


def test_scope_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "scope.json"
    path.write_text(
        '{"vault_path":"/one","vault_path":"/two",'
        '"index_root":"/index","folder_rules":{"folder":"read"}}'
    )
    with pytest.raises(ValueError):
        load_scope(path)


@pytest.mark.parametrize(
    "value",
    [
        {"id": "a", "query": "q", "expected_paths": ["/abs.md"]},
        {"id": "a", "query": "q", "expected_paths": ["../escape.md"]},
        {"id": "a", "query": "q", "expected_paths": ["."]},
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


def test_cases_reject_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id":"a","query":"first","query":"second",'
        '"expected_paths":["folder/note.md"]}\n'
    )
    with pytest.raises(ValueError):
        load_cases(path)


def test_cases_reject_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="cannot read cases JSONL"):
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


def test_private_runner_indexes_only_readable_snapshots_and_emits_safe_results(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "AI" / "denied").mkdir(parents=True)
    (vault / "AI" / "target.md").write_text("alpha beacon helpful target")
    sentinel = "UNIQUE_DENIED_NOTE_SENTINEL"
    (vault / "AI" / "denied" / "secret.md").write_text(sentinel)
    scope = {
        "vault_path": str(vault),
        "index_root": "AI",
        "folder_rules": {"AI": "read", "AI/denied": "deny"},
    }
    cases = [
        {"id": "hit", "query": "alpha beacon", "expected_paths": ["AI/target.md"]},
        {"id": "miss", "query": "unmatched quasar", "expected_paths": ["AI/target.md"]},
    ]
    result = run_private_benchmark(scope, cases)
    assert result["readable_count"] == 1
    assert result["case_results"]["hit"]["selected_paths"][0] == "AI/target.md"
    assert result["case_results"]["hit"]["relevant_hit"]
    assert not result["case_results"]["miss"]["relevant_hit"]
    serialized = json.dumps(result)
    assert sentinel not in serialized
    assert "excerpt" not in serialized and "summary" not in serialized
    assert all(
        "secret.md" not in path
        for case in result["case_results"].values()
        for path in case["selected_paths"]
    )


def test_private_runner_hashes_content_not_scope_or_cases(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    note = vault / "AI" / "one.md"
    note.write_text("original text")
    scope = {
        "vault_path": str(vault),
        "index_root": "AI",
        "folder_rules": {"AI": "read"},
    }
    cases = [{"id": "one", "query": "original", "expected_paths": ["AI/one.md"]}]
    first = run_private_benchmark(scope, cases)
    note.write_text("changed text")
    second = run_private_benchmark(scope, cases)
    assert first["corpus_sha256"] != second["corpus_sha256"]
    assert first["scope_sha256"] == second["scope_sha256"]
    assert first["cases_sha256"] == second["cases_sha256"]


def test_private_runner_rejects_missing_index_root_before_scan(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    scope = {
        "vault_path": str(vault),
        "index_root": "missing",
        "folder_rules": {"missing": "read"},
    }
    with pytest.raises(OSError):
        run_private_benchmark(scope, [])
