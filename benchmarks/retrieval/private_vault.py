"""Strict local-only input validation for the private-vault benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

_SCOPE_FIELDS = {"vault_path", "index_root", "folder_rules"}
_CASE_FIELDS = {"id", "query", "active_project", "expected_paths", "expect_empty"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    index_root = value["index_root"]
    if value["folder_rules"].get(index_root) != "read":
        raise ValueError("folder_rules must explicitly allow index_root")
    for folder in value["folder_rules"]:
        if (
            index_root != "."
            and folder != index_root
            and not folder.startswith(f"{index_root}/")
        ):
            raise ValueError("folder rule is outside index_root")
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


def run_private_benchmark(
    scope: dict[str, Any], cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """Run fixture/local cases against an isolated, readable-only vault copy."""
    from harbor_ledger_memory.catalog.database import CatalogSession, create_database
    from harbor_ledger_memory.config import (
        FolderAccess,
        FolderRule,
        MemorySettings,
        Settings,
    )
    from harbor_ledger_memory.domain.retrieval import QueryRequest
    from harbor_ledger_memory.services.query import QueryService
    from harbor_ledger_memory.services.scan import ScanService
    from harbor_ledger_memory.vault.boundary import VaultBoundary

    root = Path(scope["vault_path"]).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("vault_path must be an existing directory")
    rules = [
        FolderRule(
            path=PurePosixPath(path),
            access=FolderAccess.READ if access == "read" else FolderAccess.DENY,
        )
        for path, access in scope["folder_rules"].items()
    ]
    settings = Settings(
        vault_path=root,
        index_root=PurePosixPath(scope["index_root"]),
        folder_rules=rules,
        memory=MemorySettings(embedding_model=None),
    )
    source_boundary = VaultBoundary(settings)
    try:
        if not source_boundary.index_root.is_dir():
            raise ValueError("index_root must be an existing directory")
        digest = hashlib.sha256()
        snapshots = tuple(source_boundary.iter_admitted_snapshots())
        if not snapshots:
            raise ValueError("no readable notes admitted by the configured scope")
        readable = {snapshot.path.as_posix() for snapshot in snapshots}
        for case in cases:
            targets = case["expected_paths"]
            project = case.get("active_project")
            if any(path not in readable for path in targets) or (
                project is not None
                and not any(
                    path == project or path.startswith(f"{project}/")
                    for path in readable
                )
            ):
                raise ValueError("case labels must be within readable snapshots")
        with tempfile.TemporaryDirectory(prefix="hlm-private-benchmark-") as temp:
            vault = Path(temp) / "vault"
            vault.mkdir()
            for snapshot in sorted(snapshots, key=lambda item: item.path.as_posix()):
                relative = snapshot.path.as_posix()
                digest.update(
                    relative.encode("utf-8") + b"\0" + snapshot.content + b"\0"
                )
                destination = vault.joinpath(*snapshot.path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(snapshot.content)
            corpus_hash = digest.hexdigest()
            database_url = f"sqlite:///{Path(temp) / 'catalog.db'}"
            engine = create_database(database_url)
            session = None
            boundary = None
            try:
                session = CatalogSession(bind=engine)
                temp_settings = Settings(
                    vault_path=vault,
                    index_root=PurePosixPath(scope["index_root"]),
                    folder_rules=rules,
                    memory=MemorySettings(embedding_model=None),
                )
                boundary = VaultBoundary(temp_settings)
                ScanService(boundary, session, embedding_model=None).full_scan()
                service = QueryService(
                    session,
                    memory_settings=MemorySettings(embedding_model=None),
                    vault_path=vault,
                    path_filter=boundary.is_admitted,
                )
                results = {}
                for case in cases:
                    result = service.query(
                        QueryRequest(
                            query=case["query"],
                            active_project=case.get("active_project"),
                            include_excluded=False,
                        )
                    )
                    selected = [item.path for item in result.selected_memories[:5]]
                    expected = set(case["expected_paths"])
                    rank = next(
                        (
                            index
                            for index, path in enumerate(selected, 1)
                            if path in expected
                        ),
                        None,
                    )
                    results[case["id"]] = {
                        "selected_paths": selected,
                        "rank": rank,
                        "relevant_hit": rank is not None,
                    }
                scope_hash = hashlib.sha256(
                    json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                cases_hash = hashlib.sha256(
                    json.dumps(cases, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                return {
                    "corpus_sha256": corpus_hash,
                    "scope_sha256": scope_hash,
                    "cases_sha256": cases_hash,
                    "readable_count": len(snapshots),
                    "case_results": results,
                }
            finally:
                if session is not None:
                    session.close()
                engine.dispose()
                if boundary is not None:
                    boundary.close()
    finally:
        source_boundary.close()


def _metrics(
    result: dict[str, Any], cases: list[dict[str, Any]]
) -> dict[str, float | None]:
    ranked = [
        result["case_results"][case["id"]]["rank"]
        for case in cases
        if not case.get("expect_empty", False)
    ]
    negatives = [
        result["case_results"][case["id"]]["selected_paths"]
        for case in cases
        if case.get("expect_empty", False)
    ]
    return {
        "hit@5": sum(rank is not None for rank in ranked) / len(ranked)
        if ranked
        else None,
        "mrr": sum(1 / rank for rank in ranked if rank is not None) / len(ranked)
        if ranked
        else None,
        "negative_accuracy": sum(not paths for paths in negatives) / len(negatives)
        if negatives
        else None,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_baseline(path: Path, case_ids: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid baseline JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "scope_sha256",
        "cases_sha256",
        "corpus_sha256",
        "metrics",
        "case_results",
    }:
        raise ValueError("invalid baseline structure")
    if any(
        not isinstance(value[key], str) or not _SHA256.fullmatch(value[key])
        for key in ("scope_sha256", "cases_sha256", "corpus_sha256")
    ):
        raise ValueError("invalid baseline hash")
    metrics = value["metrics"]
    expected_metrics = {"hit@5", "mrr", "negative_accuracy"}
    if not isinstance(metrics, dict) or set(metrics) != expected_metrics:
        raise ValueError("invalid baseline metrics")
    for metric, number in metrics.items():
        if number is None and metric != "negative_accuracy":
            raise ValueError("invalid baseline metric value")
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or not 0 <= number <= 1
        ):
            raise ValueError("invalid baseline metric value")
    results = value["case_results"]
    if not isinstance(results, dict) or set(results) != case_ids:
        raise ValueError("invalid baseline case results")
    for result in results.values():
        if not isinstance(result, dict) or set(result) != {
            "rank",
            "selected_paths",
            "relevant_hit",
        }:
            raise ValueError("invalid baseline case result")
        if result["rank"] is not None and (
            isinstance(result["rank"], bool) or not isinstance(result["rank"], int)
        ):
            raise ValueError("invalid baseline rank")
        if not isinstance(result["selected_paths"], list) or any(
            not isinstance(item, str) for item in result["selected_paths"]
        ):
            raise ValueError("invalid baseline selected paths")
        if not isinstance(result["relevant_hit"], bool):
            raise ValueError("invalid baseline hit label")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    local = Path(__file__).parent / "local"
    parser.add_argument("--scope", type=Path, default=local / "scope.json")
    parser.add_argument("--cases", type=Path, default=local / "cases.jsonl")
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ("dry-run", "write-baseline", "compare-baseline"):
        actions.add_argument(f"--{action}", action="store_true")
    parser.add_argument("--confirm-replace", action="store_true")
    parser.add_argument("--show-private-details", action="store_true")
    args = parser.parse_args(argv)
    baseline_path = local / "baseline.json"
    try:
        scope, cases = load_scope(args.scope), load_cases(args.cases)
        if args.dry_run:
            # Validate root, boundary and labels without creating a catalog or querying.
            from harbor_ledger_memory.config import (
                FolderAccess,
                FolderRule,
                MemorySettings,
                Settings,
            )
            from harbor_ledger_memory.vault.boundary import VaultBoundary

            root = Path(scope["vault_path"]).expanduser().resolve(strict=True)
            settings = Settings(
                vault_path=root,
                index_root=PurePosixPath(scope["index_root"]),
                folder_rules=[
                    FolderRule(
                        path=PurePosixPath(p),
                        access=FolderAccess.READ if a == "read" else FolderAccess.DENY,
                    )
                    for p, a in scope["folder_rules"].items()
                ],
                memory=MemorySettings(embedding_model=None),
            )
            boundary = VaultBoundary(settings)
            try:
                snapshots = tuple(boundary.iter_admitted_snapshots())
                paths = {s.path.as_posix() for s in snapshots}
                if not boundary.index_root.is_dir() or not snapshots:
                    raise ValueError("configured readable scope is unavailable")
                if any(
                    any(p not in paths for p in c["expected_paths"])
                    or (
                        c.get("active_project")
                        and not any(
                            p == c["active_project"]
                            or p.startswith(c["active_project"] + "/")
                            for p in paths
                        )
                    )
                    for c in cases
                ):
                    raise ValueError("case labels must be within readable snapshots")
                corpus_hash = hashlib.sha256(
                    b"".join(
                        s.path.as_posix().encode() + b"\0" + s.content + b"\0"
                        for s in sorted(snapshots, key=lambda x: x.path.as_posix())
                    )
                ).hexdigest()
            finally:
                boundary.close()
            print(
                json.dumps(
                    {
                        "readable_count": len(snapshots),
                        "case_count": len(cases),
                        "corpus_sha256": corpus_hash,
                        "scope_sha256": hashlib.sha256(
                            json.dumps(
                                scope, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                        "cases_sha256": hashlib.sha256(
                            json.dumps(
                                cases, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                    }
                )
            )
            return 0
        if args.write_baseline and baseline_path.exists() and not args.confirm_replace:
            raise ValueError("baseline exists; use --confirm-replace to overwrite")
        if args.compare_baseline and not baseline_path.is_file():
            raise ValueError("saved baseline is required")
        baseline = (
            _load_baseline(baseline_path, {case["id"] for case in cases})
            if args.compare_baseline
            else None
        )
        result = run_private_benchmark(scope, cases)
        result["metrics"] = _metrics(result, cases)
        if args.write_baseline:
            _atomic_json(baseline_path, result)
            print(json.dumps({"written": True, "metrics": result["metrics"]}))
            return 0
        assert baseline is not None
        if (
            baseline.get("scope_sha256") != result["scope_sha256"]
            or baseline.get("cases_sha256") != result["cases_sha256"]
        ):
            raise ValueError("baseline scope or cases hash mismatch")
        changed = baseline.get("corpus_sha256") != result["corpus_sha256"]
        baseline_metrics = baseline["metrics"]
        report = {
            "vault_changed": changed,
            "baseline_metrics": baseline_metrics,
            "current_metrics": result["metrics"],
        }
        if not changed:
            report["metric_deltas"] = {
                metric: (
                    result["metrics"][metric] - baseline_metrics[metric]
                    if result["metrics"][metric] is not None
                    and baseline_metrics[metric] is not None
                    else None
                )
                for metric in result["metrics"]
            }
            report["case_deltas"] = {
                case["id"]: {
                    "before_rank": baseline["case_results"][case["id"]]["rank"],
                    "after_rank": result["case_results"][case["id"]]["rank"],
                    **(
                        {
                            "before_paths": baseline["case_results"][case["id"]][
                                "selected_paths"
                            ],
                            "after_paths": result["case_results"][case["id"]][
                                "selected_paths"
                            ],
                        }
                        if args.show_private_details
                        else {}
                    ),
                }
                for case in cases
            }
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"private benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
