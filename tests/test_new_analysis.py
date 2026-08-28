from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from bugbunny.analysis import _artifact_stage_counts, _bootstrap_metrics, _metrics
from bugbunny.build import (
    BENCHMARK_RUN_SCHEMA_VERSION,
    EVALUATION_AUDIT_SCHEMA_VERSION,
    EXPORT_INDEX_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    implementation_identity,
)


def test_metrics_materialize_generators_before_multiple_reductions() -> None:
    rows = ({"tp": 2, "fp": 0, "fn": 2} for _ in range(1))
    assert _metrics(rows)["f1"] == 2 / 3


def test_single_case_bootstrap_interval_equals_the_observed_metric() -> None:
    intervals = _bootstrap_metrics([{"tp": 2, "fp": 4, "fn": 2}], samples=20, seed=17_042)
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
    assert counts == {"tp": 1, "fp": 1, "fn": 0, "total_candidates": 2}


def test_threshold_case_ignores_matches_from_unselected_candidates() -> None:
    from bugbunny.analysis import _threshold_case

    evaluation = {
        "total_golden": 1,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.9)],
    }
    audit = [{"candidate_index": 0, "finding_id": "bb-a"}]
    counts = _threshold_case(evaluation, audit, {"bb-a": ("keep", 0.5)}, 0.92)
    assert counts == {"tp": 0, "fp": 0, "fn": 1, "total_candidates": 0}


def test_threshold_case_keeps_duplicate_texts_distinct_by_index() -> None:
    from bugbunny.analysis import _threshold_case

    evaluation = {
        "total_golden": 2,
        "pair_matches": [
            _pair("same golden", "same candidate", 0, 0, True, 0.9),
            _pair("same golden", "same candidate", 0, 1, True, 0.9),
            _pair("same golden", "same candidate", 1, 0, True, 0.9),
            _pair("same golden", "same candidate", 1, 1, True, 0.9),
        ],
    }
    audit = [
        {"candidate_index": 0, "finding_id": "bb-a"},
        {"candidate_index": 1, "finding_id": "bb-b"},
    ]
    decisions = {"bb-a": ("keep", 0.95), "bb-b": ("keep", 0.95)}

    assert _threshold_case(evaluation, audit, decisions, 0.92) == {
        "tp": 2,
        "fp": 1,
        "fn": 0,
        "total_candidates": 2,
    }


def _analysis_bundle(tmp_path, *, models=("m",), case_count=1):
    from bugbunny import __version__
    from bugbunny.benchmark import export_codereviewbench_results, load_codereviewbench_dataset
    from bugbunny.util import atomic_write_json, sha256_bytes

    run_dir = tmp_path / "run"
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    results_dir = tmp_path / "results"
    benchmark_path = tmp_path / "benchmark_data.json"
    source = {}
    for case_index in range(case_count):
        number = case_index + 1
        golden_url = f"https://github.com/example/repo/pull/{number}"
        review_url = f"https://github.com/fixtures/repo__tool__PR{number}/pull/1"
        source[golden_url] = {
            "pr_title": f"PR {number}",
            "source_repo": "repo",
            "golden_source_file": "repo.json",
            "golden_comments": [
                {"comment": f"golden issue {number}", "severity": "High", "category": "bug"}
            ],
            "reviews": [
                {
                    "tool": "fixture-tool",
                    "repo_name": f"repo__tool__PR{number}",
                    "pr_url": review_url,
                    "review_comments": [],
                }
            ],
        }
    atomic_write_json(benchmark_path, source)
    dataset = load_codereviewbench_dataset(benchmark_path, expected_case_count=case_count)
    cases = dataset.by_golden_url()
    records = []
    exports = []
    artifacts_by_model = {}
    for model_index, model in enumerate(models):
        model_artifacts = []
        for case_index, (golden_url, case) in enumerate(sorted(cases.items())):
            finding_id = "bb-" + hashlib.sha256(f"{model}:{golden_url}".encode()).hexdigest()[:20]
            fingerprint = hashlib.sha256(f"finding:{model}:{golden_url}".encode()).hexdigest()
            finding = {
                "finding_id": finding_id,
                "fingerprint": fingerprint,
                "title": "Candidate issue",
                "body": "The new branch returns stale data.",
                "path": "src/a.py",
                "side": "RIGHT",
                "line": 1,
                "end_line": 1,
                "severity": "high",
                "category": "bug",
                "confidence": 0.95,
                "verifier_confidence": 0.95,
                "evidence": "return stale",
                "root_cause": "The branch bypasses invalidation.",
                "failure_mode": "The caller receives stale data.",
                "fix_scope": "local",
                "trigger": "The new branch executes.",
                "impact": "Stale state is returned.",
                "suggested_fix": "Invalidate before returning.",
                "chunk_id": "chunk-1",
            }
            artifact = {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "tool": "bugbunny",
                "tool_version": __version__,
                "implementation": implementation_identity(),
                "status": "completed",
                "completed_at": "2026-08-26T00:00:00Z",
                "benchmark": {
                    "suite": "CodeReviewBench",
                    "case_id": case.case_id,
                    "golden_url": golden_url,
                    "review_url": case.review_url,
                    "fixture_tool": case.fixture_tool,
                    "golden_sha256": case.golden_sha256,
                    "benchmark_sha256": dataset.manifest.benchmark_sha256,
                    "dataset_golden_sha256": dataset.manifest.golden_sha256,
                },
                "config": {
                    "model": model,
                    "profile": "balanced",
                    "verifier_model": "same",
                    "min_verifier_confidence": 0.92,
                },
                "pr": {
                    "url": case.review_url,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                "runtime": {"transport": "test", "requested_model": model},
                "context": {"generation_prompt_version": "test"},
                "coverage": {"complete": True},
                "diff": {
                    "sha256": hashlib.sha256(golden_url.encode()).hexdigest(),
                    "chunk_plan_complete": True,
                    "commentable_ranges": {"RIGHT": {"src/a.py": [[1, 1]]}, "LEFT": {}},
                },
                "calls": [],
                "diagnostics": [],
                "raw_findings": [dict(finding)],
                "validated_findings": [dict(finding)],
                "rejected_findings": [],
                "findings": [dict(finding)],
            }
            artifact_path = artifact_dir / f"{model_index}-{case_index}.json"
            atomic_write_json(artifact_path, artifact)
            records.append(
                {
                    "model": model,
                    "case_id": case.case_id,
                    "artifact": str(artifact_path.relative_to(run_dir)),
                    "artifact_sha256": sha256_bytes(artifact_path.read_bytes()),
                }
            )
            model_artifacts.append(artifact)
        artifacts_by_model[model] = model_artifacts
        exports.append(
            export_codereviewbench_results(
                benchmark_path,
                model_artifacts,
                output_dir=results_dir,
                judge_model="judge_model",
                review_model=model,
                expected_case_count=case_count,
                finding_stage="generator",
            )
        )
    atomic_write_json(
        run_dir / "run_manifest.json",
        {
            "schema_version": BENCHMARK_RUN_SCHEMA_VERSION,
            "implementation": implementation_identity(),
            "records": records,
        },
    )

    judge_dir = results_dir / "judge_model"
    final_hashes = exports[-1].output_files_sha256
    index_exports = []
    for model, exported in zip(models, exports, strict=True):
        index_exports.append(
            {
                "tool_id": exported.tool_id,
                "model": model,
                "finding_stage": "generator",
                "reviews": exported.review_count,
                "candidates": exported.candidate_count,
                "manifest": str(exported.manifest_path.relative_to(results_dir)),
                "manifest_sha256": sha256_bytes(exported.manifest_path.read_bytes()),
                "candidate_audit": str(exported.candidate_audit_path.relative_to(results_dir)),
            }
        )
    atomic_write_json(
        judge_dir / "bugbunny_export_index.json",
        {
            "schema_version": EXPORT_INDEX_SCHEMA_VERSION,
            "judge_model": "judge_model",
            "implementation": implementation_identity(),
            "output_files_sha256": final_hashes,
            "exports": index_exports,
        },
    )
    candidates = json.loads((judge_dir / "candidates.json").read_text(encoding="utf-8"))
    return {
        "run_dir": run_dir,
        "results_dir": results_dir,
        "judge_dir": judge_dir,
        "golden_urls": sorted(cases),
        "tools": {model: exported.tool_id for model, exported in zip(models, exports, strict=True)},
        "candidate_text": {
            (exported.tool_id, golden_url): candidates[golden_url][exported.tool_id][0]["text"]
            for exported in exports
            for golden_url in cases
        },
        "golden_text": {
            golden_url: source[golden_url]["golden_comments"][0]["comment"] for golden_url in cases
        },
        "exports": exports,
    }


def _analysis_fixture(tmp_path, *, evaluation_row):
    from bugbunny.util import atomic_write_json

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    row = deepcopy(evaluation_row)
    for pair in row.get("pair_matches", []):
        pair["candidate"] = bundle["candidate_text"][(tool, golden_url)]
        pair["golden"] = bundle["golden_text"][golden_url]
    _bind_evaluation_identity(bundle, tool=tool, golden_url=golden_url, row=row)
    atomic_write_json(bundle["judge_dir"] / "evaluations.json", {golden_url: {tool: row}})
    return bundle


def _bind_evaluation_identity(
    bundle,
    *,
    tool,
    golden_url,
    row,
    review_timeout_seconds=1800,
):
    from bugbunny.judge import (
        JUDGE_IDENTITY_VERSION,
        judge_identity_payload,
        judged_inputs_sha256,
    )
    from bugbunny.util import canonical_json, sha256_text

    benchmark = json.loads((bundle["results_dir"] / "benchmark_data.json").read_text())
    candidates = json.loads((bundle["judge_dir"] / "candidates.json").read_text())
    groups = json.loads((bundle["judge_dir"] / "dedup_groups.json").read_text())
    identity_payload = judge_identity_payload(
        judge_model="judge_model",
        api_base="https://gateway.test/v1",
        call_timeout_seconds=30,
        review_timeout_seconds=review_timeout_seconds,
        max_attempts=5,
    )
    judge_identity = sha256_text(canonical_json(identity_payload))
    row["judge_identity_version"] = JUDGE_IDENTITY_VERSION
    row["judge_identity"] = identity_payload
    row["judge_identity_sha256"] = judge_identity
    row["judged_inputs_sha256"] = judged_inputs_sha256(
        benchmark[golden_url]["golden_comments"],
        [item["text"] for item in candidates[golden_url][tool]],
        groups.get(golden_url, {}).get(tool),
        judge_identity=judge_identity,
    )


def _evaluation_row(bundle, *, tool, golden_url, match=True, errors_count=0):
    candidate = bundle["candidate_text"][(tool, golden_url)]
    golden = bundle["golden_text"][golden_url]
    row = {
        "skipped": False,
        "tp": int(match),
        "fp": int(not match),
        "fn": int(not match),
        "errors_count": errors_count,
        "total_golden": 1,
        "total_candidates": 1,
        "precision": float(int(match)),
        "recall": float(int(match)),
        "pair_matches": [
            {
                **_pair(golden, candidate, 0, 0, match, 0.95 if match else 0.0),
                "error": "judge failed" if errors_count else None,
            }
        ],
        "true_positives": [],
        "false_negatives": [],
    }
    _bind_evaluation_identity(bundle, tool=tool, golden_url=golden_url, row=row)
    return row


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
        "precision": 1.0,
        "recall": 1.0,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.95)],
        "true_positives": [],
        "false_negatives": [],
    }
    bundle = _analysis_fixture(tmp_path, evaluation_row=clean_row)
    run_dir = bundle["run_dir"]
    results_dir = bundle["results_dir"]
    tool = bundle["tools"]["m"]
    report = analyze_evaluation(
        run_dir=run_dir,
        results_dir=results_dir,
        judge_model="judge_model",
        output_json=tmp_path / "audit-report.json",
        bootstrap_samples=10,
    )
    assert report["schema_version"] == EVALUATION_AUDIT_SCHEMA_VERSION
    assert report["implementation"] == implementation_identity()
    stored = json.loads((bundle["judge_dir"] / "evaluations.json").read_text(encoding="utf-8"))
    golden_url = bundle["golden_urls"][0]
    assert (
        report["inputs"]["judge_identity_sha256"]
        == stored[golden_url][tool]["judge_identity_sha256"]
    )
    assert report["inputs"]["judge_identity"] == stored[golden_url][tool]["judge_identity"]
    assert report["tracks"][tool]["metrics"]["f1"] == 1.0
    assert report["golden_category_counts"][tool] == {"bug": {"tp": 1, "fn": 0}}
    assert report["judge_row_hygiene"][tool]["rows_error_excluded"] == 0
    curves = report["threshold_curves_from_generator_judgments"]["m"]
    at_zero = next(point for point in curves if point["threshold"] == 0.0)
    assert at_zero["tp"] == 1 and at_zero["fp"] == 0

    error_row = deepcopy(clean_row)
    error_row.update(
        {"tp": 0, "fp": 1, "fn": 1, "errors_count": 1, "precision": 0.0, "recall": 0.0}
    )
    error_row["pair_matches"][0].update(
        {"match": False, "confidence": 0.0, "error": "judge failed"}
    )
    bundle = _analysis_fixture(tmp_path / "errored", evaluation_row=error_row)
    run_dir = bundle["run_dir"]
    results_dir = bundle["results_dir"]
    tool = bundle["tools"]["m"]
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
    assert report["judge_row_hygiene"][tool]["rows_error_excluded"] == 1
    assert report["tracks"][tool]["metrics"]["tp"] == 0


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
        "precision": 1.0,
        "recall": 1.0,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.95)],
        "true_positives": [],
        "false_negatives": [],
    }
    bundle = _analysis_fixture(tmp_path, evaluation_row=clean_row)
    run_dir = bundle["run_dir"]
    results_dir = bundle["results_dir"]
    exported = bundle["exports"][0]
    audit_path = exported.candidate_audit_path
    payload = json.loads(audit_path.read_text())
    for rows in payload["cases"].values():
        rows[0]["finding_id"] = "bb-not-in-artifact"
    audit_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = json.loads(exported.manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_audit_sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    exported.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index_path = bundle["judge_dir"] / "bugbunny_export_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["exports"][0]["manifest_sha256"] = hashlib.sha256(
        exported.manifest_path.read_bytes()
    ).hexdigest()
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(AnalysisError, match="absent"):
        analyze_evaluation(
            run_dir=run_dir,
            results_dir=results_dir,
            judge_model="judge_model",
            output_json=tmp_path / "audit-report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_rejects_index_manifest_hash_mismatch(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: _evaluation_row(bundle, tool=tool, golden_url=golden_url)}}),
        encoding="utf-8",
    )
    index_path = bundle["judge_dir"] / "bugbunny_export_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["exports"][0]["manifest_sha256"] = "0" * 64
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(AnalysisError, match="manifest no longer matches its index"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_invokes_export_manifest_verifier(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: _evaluation_row(bundle, tool=tool, golden_url=golden_url)}}),
        encoding="utf-8",
    )
    candidates_path = bundle["judge_dir"] / "candidates.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates[golden_url][tool][0]["text"] = "tampered after export"
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")

    with pytest.raises(AnalysisError, match="export manifest verification failed"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_rejects_unindexed_committed_manifest(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path, models=("m", "n"))
    golden_url = bundle["golden_urls"][0]
    tool_n = bundle["tools"]["n"]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps(
            {golden_url: {tool_n: _evaluation_row(bundle, tool=tool_n, golden_url=golden_url)}}
        ),
        encoding="utf-8",
    )
    index_path = bundle["judge_dir"] / "bugbunny_export_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["exports"] = [index["exports"][1]]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(AnalysisError, match="does not enumerate the committed manifest set"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_requires_exact_case_population(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path, case_count=2)
    tool = bundle["tools"]["m"]
    only_url = bundle["golden_urls"][0]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({only_url: {tool: _evaluation_row(bundle, tool=tool, golden_url=only_url)}}),
        encoding="utf-8",
    )

    with pytest.raises(AnalysisError, match="evaluation case population differs"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_binds_judged_candidate_text_to_audit(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    row = _evaluation_row(bundle, tool=tool, golden_url=golden_url)
    row["pair_matches"][0]["candidate"] = "a different candidate was judged"
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: row}}), encoding="utf-8"
    )

    with pytest.raises(AnalysisError, match="does not match the signed audit"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_recomputes_stored_judge_reduction(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    row = _evaluation_row(bundle, tool=tool, golden_url=golden_url)
    row["tp"] = 0
    row["fn"] = 1
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: row}}), encoding="utf-8"
    )

    with pytest.raises(AnalysisError, match="stored judge reduction differs"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_binds_run_artifacts_to_export_manifest(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation
    from bugbunny.util import sha256_bytes

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: _evaluation_row(bundle, tool=tool, golden_url=golden_url)}}),
        encoding="utf-8",
    )
    manifest_path = bundle["run_dir"] / "run_manifest.json"
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = bundle["run_dir"] / run_manifest["records"][0]["artifact"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["context"]["post_export_rerun_marker"] = True
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    run_manifest["records"][0]["artifact_sha256"] = sha256_bytes(artifact_path.read_bytes())
    manifest_path.write_text(json.dumps(run_manifest), encoding="utf-8")

    with pytest.raises(AnalysisError, match="exported artifact identity"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_rejects_run_artifact_path_escape(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: _evaluation_row(bundle, tool=tool, golden_url=golden_url)}}),
        encoding="utf-8",
    )
    manifest_path = bundle["run_dir"] / "run_manifest.json"
    run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_manifest["records"][0]["artifact"] = "../outside.json"
    manifest_path.write_text(json.dumps(run_manifest), encoding="utf-8")

    with pytest.raises(AnalysisError, match="run artifact path escapes"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_rejects_mixed_judge_identities(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path, models=("m", "n"))
    golden_url = bundle["golden_urls"][0]
    tool_m = bundle["tools"]["m"]
    tool_n = bundle["tools"]["n"]
    row_m = _evaluation_row(bundle, tool=tool_m, golden_url=golden_url)
    row_n = _evaluation_row(bundle, tool=tool_n, golden_url=golden_url)
    _bind_evaluation_identity(
        bundle,
        tool=tool_n,
        golden_url=golden_url,
        row=row_n,
        review_timeout_seconds=1801,
    )
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool_m: row_m, tool_n: row_n}}), encoding="utf-8"
    )

    with pytest.raises(AnalysisError, match="multiple judge identities"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_analyze_evaluation_rejects_internally_rehashed_foreign_judge_identity(
    tmp_path,
) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation
    from bugbunny.judge import judged_inputs_sha256
    from bugbunny.util import canonical_json, sha256_text

    bundle = _analysis_bundle(tmp_path)
    tool = bundle["tools"]["m"]
    golden_url = bundle["golden_urls"][0]
    row = _evaluation_row(bundle, tool=tool, golden_url=golden_url)
    payload = deepcopy(row["judge_identity"])
    payload["implementation"] = {
        **payload["implementation"],
        "source_sha256": "f" * 64,
    }
    foreign_identity = sha256_text(canonical_json(payload))
    row["judge_identity"] = payload
    row["judge_identity_sha256"] = foreign_identity
    benchmark = json.loads((bundle["results_dir"] / "benchmark_data.json").read_text())
    candidates = json.loads((bundle["judge_dir"] / "candidates.json").read_text())
    groups = json.loads((bundle["judge_dir"] / "dedup_groups.json").read_text())
    row["judged_inputs_sha256"] = judged_inputs_sha256(
        benchmark[golden_url]["golden_comments"],
        [item["text"] for item in candidates[golden_url][tool]],
        groups[golden_url][tool],
        judge_identity=foreign_identity,
    )
    (bundle["judge_dir"] / "evaluations.json").write_text(
        json.dumps({golden_url: {tool: row}}), encoding="utf-8"
    )

    with pytest.raises(AnalysisError, match="different judge implementation"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "report.json",
            bootstrap_samples=10,
        )


def test_allow_judge_errors_uses_reported_paired_case_intersection(tmp_path) -> None:
    from bugbunny.analysis import AnalysisError, analyze_evaluation

    bundle = _analysis_bundle(tmp_path, models=("m", "n"), case_count=2)
    first_url, second_url = bundle["golden_urls"]
    tool_m = bundle["tools"]["m"]
    tool_n = bundle["tools"]["n"]
    evaluations = {
        first_url: {
            tool_m: _evaluation_row(
                bundle,
                tool=tool_m,
                golden_url=first_url,
                match=False,
                errors_count=1,
            ),
            # This clean failure is intentionally absent from M's usable rows.
            # Pairwise metrics must not compare M's shared case against N's two
            # cases after allowing the asymmetric judge error.
            tool_n: _evaluation_row(bundle, tool=tool_n, golden_url=first_url, match=False),
        },
        second_url: {
            tool_m: _evaluation_row(bundle, tool=tool_m, golden_url=second_url),
            tool_n: _evaluation_row(bundle, tool=tool_n, golden_url=second_url),
        },
    }
    (bundle["judge_dir"] / "evaluations.json").write_text(json.dumps(evaluations), encoding="utf-8")

    with pytest.raises(AnalysisError, match="judge-error-degraded"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "strict.json",
            bootstrap_samples=10,
        )

    report = analyze_evaluation(
        run_dir=bundle["run_dir"],
        results_dir=bundle["results_dir"],
        judge_model="judge_model",
        output_json=tmp_path / "allowed.json",
        bootstrap_samples=10,
        allow_judge_errors=True,
    )
    assert report["judge_row_hygiene"][tool_m]["rows_used"] == 1
    assert report["judge_row_hygiene"][tool_n]["rows_used"] == 2
    comparison = next(iter(report["paired_model_comparisons"].values()))
    assert comparison["case_count"] == 1
    assert comparison["f1_delta"] == 0.0
    # Every comparison now runs on ONE clean-case intersection shared by all
    # compared tools, so per-pair exclusions are empty and the excluded case
    # is reported once at the top level.
    assert comparison["paired_case_exclusions"]["count"] == 0
    assert report["paired_comparison_population"] == {
        "mode": "shared_clean_case_intersection",
        "case_count": 1,
        "excluded_cases": [first_url],
    }


def test_threshold_curve_reproduces_the_stored_reduction_with_dedup_siblings() -> None:
    # The load-bearing equivalence property: at a threshold that selects every
    # exported candidate, the curve's re-reduction must equal the judge's
    # stored reduction — including dedup-sibling crediting, which only
    # matters for non-singleton groups.
    import asyncio

    from bugbunny.analysis import _threshold_case
    from bugbunny.judge import evaluate_review

    golden_comments = [{"comment": "the cache is stale", "severity": "High", "category": "bug"}]
    candidates = ["the cache is stale", "cache staleness duplicate", "unrelated comment"]
    dedup_groups = [[0, 1], [2]]

    class Judge:
        async def match_comment(self, golden: str, candidate: str) -> dict[str, object]:
            return {
                "match": candidate == "the cache is stale",
                "confidence": 0.9 if candidate == "the cache is stale" else 0.0,
                "reasoning": "deterministic",
            }

    stored = asyncio.run(evaluate_review(Judge(), golden_comments, candidates, dedup_groups))
    # Candidate 0 matches; sibling crediting marks candidate 1 as covered, so
    # only the unrelated candidate 2 is a false positive.
    assert (stored["tp"], stored["fp"], stored["fn"]) == (1, 1, 0)

    audit = [
        {"candidate_index": 0, "finding_id": "bb-a"},
        {"candidate_index": 1, "finding_id": "bb-b"},
        {"candidate_index": 2, "finding_id": "bb-c"},
    ]
    decisions = {
        "bb-a": ("keep", 0.9),
        "bb-b": ("keep", 0.9),
        "bb-c": ("keep", 0.9),
    }
    reproduced = _threshold_case(stored, audit, decisions, 0.0, dedup_groups=dedup_groups)
    assert (reproduced["tp"], reproduced["fp"], reproduced["fn"]) == (
        stored["tp"],
        stored["fp"],
        stored["fn"],
    )


def test_metrics_report_both_aggregation_conventions() -> None:
    from bugbunny.analysis import _metrics

    rows = [
        {"tp": 4, "fp": 0, "fn": 0, "total_candidates": 1},
        {"tp": 0, "fp": 0, "fn": 2, "total_candidates": 0},
    ]
    metric = _metrics(rows)
    # Upstream-faithful micro pooling: the zero-candidate case vanishes from
    # the precision denominator entirely.
    assert metric["precision"] == 1.0
    assert metric["recall"] == 4 / 6
    # Paper-convention macro weights both cases equally.
    assert metric["macro_precision"] == 0.5
    assert metric["macro_recall"] == 0.5
    assert metric["candidate_match_rate"] == 1.0


def test_stored_derived_precision_recall_must_match_the_pair_matrix(tmp_path) -> None:
    import pytest

    from bugbunny.analysis import AnalysisError, analyze_evaluation

    clean_row = {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "errors_count": 0,
        "total_golden": 1,
        "total_candidates": 1,
        "precision": 1.0,
        "recall": 1.0,
        "pair_matches": [_pair("golden text", "candidate A", 0, 0, True, 0.95)],
        "true_positives": [],
        "false_negatives": [],
    }
    bundle = _analysis_fixture(tmp_path / "tampered", evaluation_row=clean_row)
    evaluations_path = bundle["judge_dir"] / "evaluations.json"
    stored = json.loads(evaluations_path.read_text(encoding="utf-8"))
    golden_url = bundle["golden_urls"][0]
    tool = bundle["tools"]["m"]
    stored[golden_url][tool]["precision"] = 0.25
    evaluations_path.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(AnalysisError, match="stored judge precision differs"):
        analyze_evaluation(
            run_dir=bundle["run_dir"],
            results_dir=bundle["results_dir"],
            judge_model="judge_model",
            output_json=tmp_path / "audit.json",
            bootstrap_samples=10,
        )
