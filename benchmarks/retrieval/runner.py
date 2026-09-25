"""Deterministic, offline retrieval benchmark runner.

The benchmark deliberately uses lexical retrieval only. It owns no production
retrieval code and never downloads models or contacts a network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.config import MemorySettings, Settings
from harbor_ledger_memory.domain.retrieval import QueryRequest, QuerySettings
from harbor_ledger_memory.services.query import QueryService
from harbor_ledger_memory.services.scan import ScanService

ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = ROOT / "benchmarks" / "retrieval" / "corpus"
CASES_PATH = ROOT / "benchmarks" / "retrieval" / "cases.jsonl"
BASELINE_PATH = ROOT / "benchmarks" / "retrieval" / "baseline.json"
REQUIRED_COUNTS = {
    "direct_lexical": 12,
    "paraphrase": 12,
    "active_project": 8,
    "graph_supported": 8,
    "negative_ambiguous": 5,
    "excerpt_evidence": 5,
}
SPLITS = ("dev", "heldout")
METRIC_FIELDS = {
    "hit_at_5",
    "mrr",
    "evidence_at_5",
    "negative_accuracy",
    "latency_p50_ms",
    "latency_p95_ms",
}


@dataclass(frozen=True)
class Metrics:
    hit_at_5: float
    mrr: float
    evidence_at_5: float
    negative_accuracy: float
    latency_p50_ms: float
    latency_p95_ms: float


@dataclass(frozen=True)
class BenchmarkResult:
    corpus_sha256: str
    cases_sha256: str
    selected_paths: dict[str, list[str]]
    ranks: dict[str, int | None]
    evidence_hits: dict[str, bool]
    metrics: Metrics


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def corpus_hash(corpus_dir: Path = CORPUS_DIR) -> str:
    digest = hashlib.sha256()
    for path in sorted(corpus_dir.glob("*.md")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def cases_hash(cases: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        cases, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_cases(cases: list[dict[str, Any]]) -> None:
    if len(cases) < 50:
        raise ValueError("benchmark requires at least 50 cases")
    if len({case.get("id") for case in cases}) != len(cases):
        raise ValueError("benchmark case IDs must be unique")
    counts = {
        kind: sum(case.get("kind") == kind for case in cases)
        for kind in REQUIRED_COUNTS
    }
    if counts != REQUIRED_COUNTS:
        raise ValueError(f"benchmark case counts mismatch: {counts}")
    for case in cases:
        if case.get("split") not in SPLITS:
            raise ValueError(f"case {case.get('id')} has an invalid split")
        required = {"id", "split", "kind", "query", "relevant", "expect_empty"}
        if not required <= case.keys():
            raise ValueError(f"case {case.get('id')} is missing required fields")
        if not isinstance(case["relevant"], list):
            raise ValueError(f"case {case['id']} relevant must be a list")
        for item in cast(list[dict[str, Any]], case["relevant"]):
            if set(item) != {"path", "evidence"}:
                raise ValueError(f"case {case['id']} has invalid relevant item")
    for kind in REQUIRED_COUNTS:
        if {case["split"] for case in cases if case["kind"] == kind} != set(SPLITS):
            raise ValueError(f"case kind {kind} must appear in every split")


def benchmark_query_settings() -> QuerySettings:
    """Use production retrieval defaults for benchmark runs."""
    return QuerySettings()


def validate_baseline() -> None:
    cases = load_cases()
    validate_cases(cases)
    baseline = json.loads(BASELINE_PATH.read_text())
    if baseline.get("version") != 3:
        raise ValueError("unsupported benchmark baseline version")
    if baseline.get("corpus_sha256") != corpus_hash():
        raise ValueError("benchmark corpus hash does not match baseline")
    if baseline.get("cases_sha256") != cases_hash(cases):
        raise ValueError("benchmark case definition does not match baseline")
    split_payloads_raw = baseline.get("splits")
    if not isinstance(split_payloads_raw, dict) or set(
        cast(dict[str, Any], split_payloads_raw)
    ) != set(SPLITS):
        raise ValueError("benchmark baseline split schema is invalid")
    split_payloads_any = cast(dict[str, Any], split_payloads_raw)
    split_payloads = cast(dict[str, dict[str, Any]], split_payloads_raw)
    for split in SPLITS:
        payload_raw = split_payloads_any[split]
        if not isinstance(payload_raw, dict):
            raise ValueError("benchmark baseline split schema is invalid")
        payload = split_payloads[split]
        metrics_raw = payload.get("metrics")
        if not isinstance(metrics_raw, dict):
            raise ValueError("benchmark baseline metric schema is invalid")
        metrics = cast(dict[str, Any], metrics_raw)
        if set(metrics) != METRIC_FIELDS or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in metrics.values()
        ):
            raise ValueError("benchmark baseline metric schema is invalid")
        expected_ids = {case["id"] for case in cases if case["split"] == split}
        actual_ids = set(payload.get("case_results", {}))
        if actual_ids != expected_ids:
            raise ValueError("benchmark baseline case IDs do not match cases")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((percentile / 100) * len(ordered))))
    return round(ordered[index], 3)


def _run_case(
    case: dict[str, Any], corpus: Path
) -> tuple[list[str], int | None, bool, float]:
    with tempfile.TemporaryDirectory(prefix="hlm-benchmark-") as temp:
        vault = Path(temp) / "vault"
        shutil.copytree(corpus, vault)
        settings = Settings(
            vault_path=vault,
            database_url=f"sqlite:///{Path(temp) / 'catalog.db'}",
            memory=MemorySettings(embedding_model=None),
            retrieval=benchmark_query_settings(),
        )
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            ScanService.from_settings(settings).full_scan()
            started = time.perf_counter()
            result = QueryService(
                session,
                retrieval_settings=settings.retrieval,
                memory_settings=settings.memory,
            ).query(
                QueryRequest(
                    query=case["query"],
                    active_project=case.get("active_project"),
                    include_excluded=False,
                )
            )
            elapsed = (time.perf_counter() - started) * 1000
            selected = [memory.path for memory in result.selected_memories[:5]]
            relevant = {item["path"]: item["evidence"] for item in case["relevant"]}
            rank = next(
                (index for index, path in enumerate(selected, 1) if path in relevant),
                None,
            )
            evidence_hit = any(
                memory.path in relevant
                and relevant[memory.path].lower() in memory.excerpt.lower()
                for memory in result.selected_memories[:5]
            )
            return selected, rank, evidence_hit, elapsed
        finally:
            session.close()
            engine.dispose()


def run_benchmark(
    case_ids: list[str] | None = None, split: str | None = None
) -> BenchmarkResult:
    cases = load_cases()
    validate_cases(cases)
    if split is not None and split not in SPLITS:
        raise ValueError(f"unknown benchmark split: {split}")
    selected_cases = [
        case
        for case in cases
        if (split is None or case["split"] == split)
        and (case_ids is None or case["id"] in case_ids)
    ]
    if case_ids is not None and len(selected_cases) != len(case_ids):
        raise ValueError("requested benchmark case does not exist")
    paths: dict[str, list[str]] = {}
    ranks: dict[str, int | None] = {}
    evidence: dict[str, bool] = {}
    latencies: list[float] = []
    negative_hits = 0
    reciprocal_ranks: list[float] = []
    hit_count = 0
    evidence_count = 0
    for case in selected_cases:
        selected, rank, evidence_hit, elapsed = _run_case(case, CORPUS_DIR)
        paths[case["id"]] = selected
        ranks[case["id"]] = rank
        evidence[case["id"]] = evidence_hit
        latencies.append(elapsed)
        if not case["expect_empty"]:
            if rank is not None:
                hit_count += 1
                reciprocal_ranks.append(1 / rank)
            if evidence_hit:
                evidence_count += 1
        if case["expect_empty"] and case["expect_empty"] == (not selected):
            negative_hits += 1
    ranking_cases = [case for case in selected_cases if not case["expect_empty"]]
    total = len(ranking_cases)
    negative_total = sum(case["expect_empty"] for case in selected_cases)
    metrics = Metrics(
        hit_at_5=round(hit_count / total, 4) if total else 0.0,
        mrr=round(sum(reciprocal_ranks) / total, 4) if total else 0.0,
        evidence_at_5=round(evidence_count / total, 4) if total else 0.0,
        negative_accuracy=round(negative_hits / negative_total, 4)
        if negative_total
        else 0.0,
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
    )
    return BenchmarkResult(
        corpus_hash(), cases_hash(cases), paths, ranks, evidence, metrics
    )


def _result_payload(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "metrics": asdict(result.metrics),
        "case_results": {
            case_id: {
                "selected_paths": result.selected_paths[case_id],
                "rank": result.ranks[case_id],
                "evidence_hit": result.evidence_hits[case_id],
            }
            for case_id in result.selected_paths
        },
    }


def _baseline_payload(results: dict[str, BenchmarkResult]) -> dict[str, Any]:
    first = next(iter(results.values()))
    return {
        "version": 3,
        "profile": "lexical",
        "corpus_sha256": first.corpus_sha256,
        "cases_sha256": first.cases_sha256,
        "splits": {split: _result_payload(result) for split, result in results.items()},
    }


def compare_candidate_to_baseline(
    results: dict[str, BenchmarkResult], baseline: dict[str, Any]
) -> None:
    """Reject changes to per-case outcomes; baseline rewrites are explicit."""
    for split in SPLITS:
        expected = baseline["splits"][split]["case_results"]
        actual = _result_payload(results[split])["case_results"]
        if actual != expected:
            raise ValueError(f"benchmark case result changed ({split})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--compare-baseline", action="store_true")
    args = parser.parse_args()
    results = {split: run_benchmark(split=split) for split in SPLITS}
    if args.write_baseline:
        BASELINE_PATH.write_text(
            json.dumps(_baseline_payload(results), indent=2) + "\n"
        )
    if args.compare_baseline:
        validate_baseline()
        baseline = json.loads(BASELINE_PATH.read_text())
        for split, result in results.items():
            for metric, expected in baseline["splits"][split]["metrics"].items():
                if metric.startswith("latency"):
                    continue
                if getattr(result.metrics, metric) < expected:
                    raise SystemExit(f"benchmark regression ({split}): {metric}")
            repeat = run_benchmark(split=split)
            if result.selected_paths != repeat.selected_paths:
                raise SystemExit(
                    f"benchmark nondeterminism ({split}): selected paths changed"
                )
        compare_candidate_to_baseline(results, baseline)
    baseline = json.loads(BASELINE_PATH.read_text()) if BASELINE_PATH.exists() else None
    output: dict[str, Any] = {"profile": "lexical", "splits": {}}
    for split, result in results.items():
        output["splits"][split] = {
            "candidate": asdict(result.metrics),
            "baseline": baseline["splits"][split]["metrics"] if baseline else None,
        }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
