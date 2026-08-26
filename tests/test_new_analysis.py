from __future__ import annotations

import pytest

from bugbunny.analysis import _artifact_stage_counts, _bootstrap_metrics, _metrics


def test_metrics_materialize_generators_before_multiple_reductions() -> None:
    rows = ({"tp": 2, "fp": 0, "fn": 2} for _ in range(1))
    assert _metrics(rows)["f1"] == 2 / 3


def test_single_case_bootstrap_interval_equals_the_observed_metric() -> None:
    intervals = _bootstrap_metrics(
        [{"tp": 2, "fp": 4, "fn": 2}], samples=20, seed=17_042
    )
    assert intervals["precision"] == pytest.approx([1 / 3, 1 / 3])
    assert intervals["recall"] == pytest.approx([0.5, 0.5])
    assert intervals["f1"] == pytest.approx([0.4, 0.4])


def test_stage_counts_separate_prompt_discovery_and_index_pressure() -> None:
    artifacts = [
        {
            "raw_findings": [],
            "validated_findings": [],
            "findings": [],
            "context": {
                "effective_context_files_exposed_to_model": ["src/a.py"],
                "context_pressure": {
                    "generation_contexts_clipped_to_prompt_budget": 1,
                    "largest_context_budget_utilization": 0.97,
                    "largest_verifier_input_char_budget_utilization": 0.42,
                    "selection_bound_hits": {
                        "round_limit_hit": 1,
                        "repository_index_truncated": 1,
                    },
                },
            },
            "calls": [],
            "diagnostics": [],
        },
        {
            "raw_findings": [],
            "validated_findings": [],
            "findings": [],
            "context": {
                "effective_context_files_exposed_to_model": ["src/b.py", "tests/test_b.py"],
                "context_pressure": {
                    "largest_context_budget_utilization": 0.11,
                    "largest_verifier_input_char_budget_utilization": 0.25,
                    "selection_bound_hits": {"repository_index_truncated": 1},
                },
            },
            "calls": [],
            "diagnostics": [],
        },
    ]

    counts = _artifact_stage_counts(artifacts)

    assert counts["reviews_hitting_prompt_or_evidence_bound"] == 1
    assert counts["reviews_hitting_discovery_bound"] == 1
    assert counts["reviews_with_hierarchical_index_summary"] == 2
    assert counts["discovery_bound_hits_by_reason"]["round_limit_hit"] == 1
    assert counts["generation_budget_utilization_mean"] == pytest.approx(0.54)
    assert counts["generation_budget_utilization_max"] == pytest.approx(0.97)
    assert counts["verifier_budget_utilization_max"] == pytest.approx(0.42)


def _pair(golden, candidate, golden_index, candidate_index, match, confidence):
    return {
        "golden": golden,
        "golden_index": golden_index,
        "candidate": candidate,
        "candidate_index": candidate_index,
        "match": match,
        "confidence": confidence,
        "error": None,
    }


def test_threshold_case_mirrors_the_judges_greedy_reduction() -> None:
    from bugbunny.analysis import _threshold_case

    # Candidate A (0.9, judged first) claims the golden; candidate B also
    # matches but never beats the best confidence, so the official reduction
    # counts B as a false positive. The reconstruction must agree.
    evaluation = {
        "total_golden": 1,
        "pair_matches": [
            _pair("golden text", "candidate A", 0, 0, True, 0.9),
            _pair("golden text", "candidate B", 0, 1, True, 0.5),
        ],
    }
    audit = [
        {"candidate_index": 0, "finding_id": "bb-a"},
        {"candidate_index": 1, "finding_id": "bb-b"},
    ]
    decisions = {"bb-a": ("keep", 0.95), "bb-b": ("keep", 0.95)}
    counts = _threshold_case(evaluation, audit, decisions, 0.92)
    assert counts == {"tp": 1, "fp": 1, "fn": 0}


def test_threshold_case_ignores_matches_from_unselected_candidates() -> None:
    from bugbunny.analysis import _threshold_case

    evaluation = {
        "total_golden": 1,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.9)],
    }
    audit = [{"candidate_index": 0, "finding_id": "bb-a"}]
    counts = _threshold_case(evaluation, audit, {"bb-a": ("keep", 0.5)}, 0.92)
    assert counts == {"tp": 0, "fp": 0, "fn": 1}


def _analysis_fixture(tmp_path, *, evaluation_row):

    from bugbunny.util import atomic_write_json, sha256_bytes

    run_dir = tmp_path / "run"
    results_dir = tmp_path / "results"
    judge_dir = results_dir / "judge_model"
    judge_dir.mkdir(parents=True)
    golden_url = "https://github.com/example/repo/pull/1"
    artifact = {
        "schema_version": "bugbunny-review-v2",
        "status": "completed",
        "benchmark": {"golden_url": golden_url},
        "config": {"model": "m", "min_verifier_confidence": 0.92},
        "context": {},
        "calls": [],
        "diagnostics": [],
        "raw_findings": [{"finding_id": "bb-a"}],
        "validated_findings": [{"finding_id": "bb-a", "verifier_confidence": 0.95}],
        "rejected_findings": [],
        "findings": [{"finding_id": "bb-a", "verifier_confidence": 0.95}],
    }
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    artifact_path = artifact_dir / "case.json"
    atomic_write_json(artifact_path, artifact)
    atomic_write_json(
        run_dir / "run_manifest.json",
        {
            "records": [
                {
                    "model": "m",
                    "case_id": "case",
                    "artifact": "artifacts/case.json",
                    "artifact_sha256": sha256_bytes(artifact_path.read_bytes()),
                }
            ]
        },
    )
    atomic_write_json(
        judge_dir / "bugbunny_export_index.json",
        {
            "exports": [
                {
                    "tool_id": "tool-gen",
                    "model": "m",
                    "finding_stage": "generator",
                    "candidates": 1,
                    "candidate_audit": "judge_model/audit.json",
                }
            ]
        },
    )
    atomic_write_json(
        judge_dir / "audit.json",
        {"cases": {golden_url: [{"candidate_index": 0, "finding_id": "bb-a"}]}},
    )
    atomic_write_json(judge_dir / "evaluations.json", {golden_url: {"tool-gen": evaluation_row}})
    return run_dir, results_dir


def test_analyze_evaluation_end_to_end_and_error_row_hygiene(tmp_path) -> None:
    import pytest

    from bugbunny.analysis import AnalysisError, analyze_evaluation

    clean_row = {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "errors_count": 0,
        "total_golden": 1,
        "total_candidates": 1,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.95)],
        "true_positives": [],
        "false_negatives": [],
    }
    run_dir, results_dir = _analysis_fixture(tmp_path, evaluation_row=clean_row)
    report = analyze_evaluation(
        run_dir=run_dir,
        results_dir=results_dir,
        judge_model="judge_model",
        output_json=tmp_path / "audit-report.json",
        bootstrap_samples=10,
    )
    assert report["tracks"]["tool-gen"]["metrics"]["f1"] == 1.0
    assert report["judge_row_hygiene"]["tool-gen"]["rows_error_excluded"] == 0
    curves = report["threshold_curves_from_generator_judgments"]["m"]
    at_zero = next(point for point in curves if point["threshold"] == 0.0)
    assert at_zero["tp"] == 1 and at_zero["fp"] == 0

    error_row = dict(clean_row, errors_count=3)
    run_dir, results_dir = _analysis_fixture(tmp_path / "errored", evaluation_row=error_row)
    with pytest.raises(AnalysisError, match="judge-error-degraded"):
        analyze_evaluation(
            run_dir=run_dir,
            results_dir=results_dir,
            judge_model="judge_model",
            output_json=tmp_path / "audit-report-2.json",
            bootstrap_samples=10,
        )
    report = analyze_evaluation(
        run_dir=run_dir,
        results_dir=results_dir,
        judge_model="judge_model",
        output_json=tmp_path / "audit-report-3.json",
        bootstrap_samples=10,
        allow_judge_errors=True,
    )
    assert report["judge_row_hygiene"]["tool-gen"]["rows_error_excluded"] == 1
    assert report["tracks"]["tool-gen"]["metrics"]["tp"] == 0


def test_analyze_evaluation_rejects_audit_artifact_mismatch(tmp_path) -> None:
    import json

    import pytest

    from bugbunny.analysis import AnalysisError, analyze_evaluation

    clean_row = {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "errors_count": 0,
        "total_golden": 1,
        "total_candidates": 1,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.95)],
        "true_positives": [],
        "false_negatives": [],
    }
    run_dir, results_dir = _analysis_fixture(tmp_path, evaluation_row=clean_row)
    audit_path = results_dir / "judge_model" / "audit.json"
    payload = json.loads(audit_path.read_text())
    for rows in payload["cases"].values():
        rows[0]["finding_id"] = "bb-not-in-artifact"
    audit_path.write_text(json.dumps(payload))
    with pytest.raises(AnalysisError, match="absent"):
        analyze_evaluation(
            run_dir=run_dir,
            results_dir=results_dir,
            judge_model="judge_model",
            output_json=tmp_path / "audit-report.json",
            bootstrap_samples=10,
        )
