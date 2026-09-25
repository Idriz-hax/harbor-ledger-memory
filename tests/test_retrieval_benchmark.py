"""Offline retrieval benchmark integrity and regression coverage."""

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.retrieval import runner
from benchmarks.retrieval.runner import (
    CASES_PATH,
    CORPUS_DIR,
    load_cases,
    run_benchmark,
    validate_baseline,
)


def test_benchmark_fixture_has_required_shape() -> None:
    cases = load_cases(CASES_PATH)
    assert 50 <= len(cases)
    assert len({case["id"] for case in cases}) == len(cases)
    assert CORPUS_DIR.is_dir()
    assert len(list(CORPUS_DIR.glob("*.md"))) >= 25
    counts = {
        kind: sum(case["kind"] == kind for case in cases)
        for kind in {
            "direct_lexical",
            "paraphrase",
            "active_project",
            "graph_supported",
            "negative_ambiguous",
            "excerpt_evidence",
        }
    }
    assert counts == {
        "direct_lexical": 12,
        "paraphrase": 12,
        "active_project": 8,
        "graph_supported": 8,
        "negative_ambiguous": 5,
        "excerpt_evidence": 5,
    }
    assert {case["split"] for case in cases} == {"dev", "heldout"}
    for kind in counts:
        assert {case["split"] for case in cases if case["kind"] == kind} == {
            "dev",
            "heldout",
        }


def test_benchmark_uses_production_seed_limit() -> None:
    from harbor_ledger_memory.domain.retrieval import QuerySettings

    assert (
        runner.benchmark_query_settings().max_seed_nodes
        == QuerySettings().max_seed_nodes
    )


def test_lexical_benchmark_subset_is_offline_and_deterministic() -> None:
    first = run_benchmark(case_ids=["lexical-01", "paraphrase-01"])
    second = run_benchmark(case_ids=["lexical-01", "paraphrase-01"])
    assert first.selected_paths == second.selected_paths
    assert first.metrics.hit_at_5 >= 0.5
    assert first.metrics.latency_p50_ms >= 0
    assert first.metrics.latency_p95_ms >= first.metrics.latency_p50_ms


def test_committed_baseline_matches_fixture() -> None:
    validate_baseline()


def test_negative_accuracy_uses_negative_cases_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_case(
        case: dict[str, Any], corpus: object
    ) -> tuple[list[str], int | None, bool, float]:
        if case["expect_empty"]:
            return [], None, False, 1.0
        return ["ledger-export.md"], 1, False, 1.0

    monkeypatch.setattr(runner, "_run_case", fake_run_case)
    result = run_benchmark(case_ids=["lexical-01", "negative-01"])
    assert result.metrics.negative_accuracy == 1.0
    assert result.metrics.hit_at_5 == 1.0
    assert result.metrics.mrr == 1.0


def test_baseline_rejects_changed_case_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_path = tmp_path / "cases.jsonl"
    baseline_path = tmp_path / "baseline.json"
    cases_path.write_text(CASES_PATH.read_text())
    baseline_path.write_text(runner.BASELINE_PATH.read_text())
    lines = cases_path.read_text().splitlines()
    changed = json.loads(lines[0])
    changed["query"] = "changed definition with the same ID"
    lines[0] = json.dumps(changed)
    cases_path.write_text("\n".join(lines) + "\n")
    changed_cases = [json.loads(line) for line in lines]
    monkeypatch.setattr(runner, "load_cases", lambda: changed_cases)
    monkeypatch.setattr(runner, "BASELINE_PATH", baseline_path)

    with pytest.raises(ValueError, match="case definition"):
        runner.validate_baseline()


def test_baseline_rejects_invalid_metric_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline = json.loads(runner.BASELINE_PATH.read_text())
    del baseline["splits"]["dev"]["metrics"]["mrr"]
    baseline_path.write_text(json.dumps(baseline))
    monkeypatch.setattr(runner, "BASELINE_PATH", baseline_path)

    with pytest.raises(ValueError, match="metric schema"):
        runner.validate_baseline()


def test_baseline_rejects_case_result_change_even_when_metrics_match() -> None:
    results = {split: run_benchmark(split=split) for split in runner.SPLITS}
    for field, value in (
        ("selected_paths", ["changed.md"]),
        ("rank", 99),
        ("evidence_hit", False),
    ):
        baseline = runner._baseline_payload(results)
        case_id = next(iter(baseline["splits"]["dev"]["case_results"]))
        baseline["splits"]["dev"]["case_results"][case_id][field] = value

        with pytest.raises(ValueError, match="case result"):
            runner.compare_candidate_to_baseline(results, baseline)
