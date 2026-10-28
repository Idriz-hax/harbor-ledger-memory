"""Strict local-only input validation for the private-vault benchmark."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

_SCOPE_FIELDS = {"vault_path", "index_root", "folder_rules"}
_CASE_FIELDS = {"id", "query", "active_project", "expected_paths", "expect_empty"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _relative_path(value: Any, label: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a vault-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in value.split("/"):
        raise ValueError(f"{label} must be a vault-relative POSIX path")
    if value == "." and allow_root:
        return "."
    if any(part in {"", "."} for part in value.split("/")):
        raise ValueError(f"{label} must be a vault-relative POSIX path")
    return path.as_posix()


def load_scope(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid scope JSON") from exc
    if not isinstance(value, dict) or set(value) != _SCOPE_FIELDS:
        raise ValueError(
            "scope must contain exactly vault_path, index_root, folder_rules"
        )
    for key in ("vault_path", "index_root"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"{key} must be a nonempty path string")
    value["index_root"] = _relative_path(
        value["index_root"], "index_root", allow_root=True
    )
    rules = value["folder_rules"]
    if not isinstance(rules, dict) or not rules:
        raise ValueError("folder_rules must be a nonempty object")
    value["folder_rules"] = {
        _relative_path(folder, "folder rule", allow_root=True): rule
        for folder, rule in rules.items()
    }
    for rule in value["folder_rules"].values():
        if not isinstance(rule, str) or rule not in {"read", "deny"}:
            raise ValueError("folder rules may only be read or deny")
    return value


def load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read cases JSONL") from exc
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    for number, line in enumerate(lines, 1):
        try:
            case = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"malformed JSON on line {number}") from exc
        if not isinstance(case, dict) or set(case) - _CASE_FIELDS:
            raise ValueError(f"invalid case fields on line {number}")
        if not {"id", "query", "expected_paths"} <= set(case):
            raise ValueError(f"missing case fields on line {number}")
        ident, query, expected = case["id"], case["query"], case["expected_paths"]
        if not isinstance(ident, str) or not _ID.fullmatch(ident) or ident in ids:
            raise ValueError(f"invalid or duplicate case id on line {number}")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError(f"invalid query on line {number}")
        if not isinstance(expected, list):
            raise ValueError(f"expected_paths must be a list on line {number}")
        case["expected_paths"] = [
            _relative_path(item, "expected path") for item in expected
        ]
        empty = case.get("expect_empty", False)
        if (
            not isinstance(empty, bool)
            or (empty and expected)
            or (not empty and not expected)
        ):
            raise ValueError(f"inconsistent negative label on line {number}")
        if "active_project" in case and case["active_project"] is not None:
            case["active_project"] = _relative_path(
                case["active_project"], "active_project"
            )
        ids.add(ident)
        cases.append(case)
    return cases
