"""Post-hoc evaluation diagnostics that never change benchmark scoring."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean
from typing import Any

from bugbunny.benchmark import sanitize_model_name, verify_codereviewbench_export_manifest
from bugbunny.build import (
    BENCHMARK_RUN_SCHEMA_VERSION,
    CANDIDATE_AUDIT_SCHEMA_VERSION,
    EVALUATION_AUDIT_SCHEMA_VERSION,
    EXPORT_INDEX_SCHEMA_VERSION,
    implementation_identity,
)
from bugbunny.judge import (
    JUDGE_IDENTITY_VERSION,
    judged_inputs_sha256,
    validate_judge_identity_payload,
)
from bugbunny.util import (
    atomic_write_json,
    canonical_json,
    is_finite_number,
    load_json,
    sha256_bytes,
    sha256_text,
    utc_now,
)


class AnalysisError(ValueError):
    """An evaluation audit input is absent or internally inconsistent."""


_PROMPT_BOUND_FIELDS = (
    "batches_at_or_above_95_percent_of_context_budget",
    "generation_contexts_clipped_to_prompt_budget",
    "verifier_contexts_clipped_to_evidence_budget",
    "verifier_contexts_clipped_to_prompt_budget",
    "verifier_evidence_batches_clipped_to_any_budget",
)

# Repository-index summarization is reported separately.  A hierarchical index
# can be summarized while the complete immutable inventory remains available
# through pageable list actions, so treating that condition as a context loss
# would overstate harness pressure.
_DISCOVERY_BOUND_FIELDS = (
    "blob_read_limit_hit",
    "context_limit_hit",
    "file_limit_hit",
    "list_pagination_unresolved",
    "repository_inventory_omission_hit",
    "request_limit_hit",
    "round_limit_hit",
    "search_offset_limit_hit",
    "search_pagination_unresolved",
    "search_scan_limit_hit",
    "selector_observations_truncated",
)


def _object(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise AnalysisError(f"expected a JSON object: {path}")
    return value


def _resolved_child(root: Path, relative: Any, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise AnalysisError(f"{label} must be a relative path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AnalysisError(f"{label} escapes its declared root") from exc
    return path


def _bind_judged_candidate_inputs(
    *,
    tool: str,
    golden_url: str,
    evaluation: Mapping[str, Any],
    audit: Sequence[Mapping[str, Any]],
    golden_comments: Sequence[Mapping[str, Any]],
    candidates: Sequence[str],
    dedup_groups: Any,
) -> tuple[bool, ...] | None:
    """Prove the signed audit, pair matrix, and stored reduction agree."""

    if evaluation.get("skipped"):
        # Skipped rows contain no pair matrix by definition and never enter any
        # metric.  Their exported candidates remain bound by manifest
        # verification; there is no claim that the judge consumed them.
        return None
    total_candidates = evaluation.get("total_candidates")
    if (
        not isinstance(total_candidates, int)
        or isinstance(total_candidates, bool)
        or total_candidates < 0
        or total_candidates != len(audit)
        or total_candidates != len(candidates)
    ):
        raise AnalysisError(
            f"candidate audit for {golden_url} / {tool} does not match total_candidates"
        )
    audit_by_index: dict[int, Mapping[str, Any]] = {}
    for item in audit:
        candidate_index = item.get("candidate_index")
        if (
            not isinstance(candidate_index, int)
            or isinstance(candidate_index, bool)
            or candidate_index < 0
            or candidate_index >= total_candidates
            or candidate_index in audit_by_index
        ):
            raise AnalysisError(f"candidate audit indexes are invalid for {golden_url} / {tool}")
        candidate_hash = item.get("candidate_sha256")
        if not isinstance(candidate_hash, str) or len(candidate_hash) != 64:
            raise AnalysisError(f"candidate audit has no valid text hash for {golden_url} / {tool}")
        audit_by_index[candidate_index] = item
    if set(audit_by_index) != set(range(total_candidates)):
        raise AnalysisError(f"candidate audit indexes are incomplete for {golden_url} / {tool}")

    raw_pairs = evaluation.get("pair_matches")
    total_golden = evaluation.get("total_golden")
    if (
        not isinstance(raw_pairs, list)
        or not isinstance(total_golden, int)
        or isinstance(total_golden, bool)
        or total_golden < 0
        or total_golden != len(golden_comments)
    ):
        raise AnalysisError(f"judge pair matrix is malformed for {golden_url} / {tool}")
    expected_coordinates = {
        (golden_index, candidate_index)
        for golden_index in range(total_golden)
        for candidate_index in range(total_candidates)
    }
    expected_order = [
        (golden_index, candidate_index)
        for golden_index in range(total_golden)
        for candidate_index in range(total_candidates)
    ]
    sibling_map: dict[int, set[int]] = {}
    assigned_group_indexes: set[int] = set()
    if dedup_groups is not None:
        if not isinstance(dedup_groups, list):
            raise AnalysisError(f"dedup groups are malformed for {golden_url} / {tool}")
        for raw_group in dedup_groups:
            if not isinstance(raw_group, list) or not raw_group:
                raise AnalysisError(f"dedup groups are malformed for {golden_url} / {tool}")
            group: set[int] = set()
            for candidate_index in raw_group:
                if (
                    not isinstance(candidate_index, int)
                    or isinstance(candidate_index, bool)
                    or candidate_index < 0
                    or candidate_index >= total_candidates
                    or candidate_index in group
                    or candidate_index in assigned_group_indexes
                ):
                    raise AnalysisError(
                        f"dedup group indexes are invalid for {golden_url} / {tool}"
                    )
                group.add(candidate_index)
                assigned_group_indexes.add(candidate_index)
            for candidate_index in group:
                sibling_map[candidate_index] = group - {candidate_index}

    coordinates: set[tuple[int, int]] = set()
    candidate_text_by_index: dict[int, str] = {}
    golden_best_confidence = [0.0] * total_golden
    golden_matched = [False] * total_golden
    candidate_matched = [False] * total_candidates
    error_count = 0
    for pair_position, raw_pair in enumerate(raw_pairs):
        if not isinstance(raw_pair, Mapping):
            raise AnalysisError(f"judge pair matrix is malformed for {golden_url} / {tool}")
        golden_index = raw_pair.get("golden_index")
        candidate_index = raw_pair.get("candidate_index")
        golden_text = raw_pair.get("golden")
        candidate_text = raw_pair.get("candidate")
        if (
            not isinstance(golden_index, int)
            or isinstance(golden_index, bool)
            or not isinstance(candidate_index, int)
            or isinstance(candidate_index, bool)
            or not isinstance(golden_text, str)
            or not isinstance(candidate_text, str)
        ):
            raise AnalysisError(f"judge pair matrix is malformed for {golden_url} / {tool}")
        coordinate = (golden_index, candidate_index)
        if coordinate not in expected_coordinates or coordinate in coordinates:
            raise AnalysisError(f"judge pair coordinates are invalid for {golden_url} / {tool}")
        if pair_position >= len(expected_order) or coordinate != expected_order[pair_position]:
            raise AnalysisError(f"judge pair order is invalid for {golden_url} / {tool}")
        coordinates.add(coordinate)
        if golden_text != golden_comments[golden_index].get("comment"):
            raise AnalysisError(f"judge pair matrix changes golden text for {golden_url} / {tool}")
        if candidate_text != candidates[candidate_index]:
            raise AnalysisError(
                "judge pair candidate text does not match the signed audit "
                f"for {golden_url} / {tool}"
            )
        prior_text = candidate_text_by_index.setdefault(candidate_index, candidate_text)
        if prior_text != candidate_text:
            raise AnalysisError(
                "judge pair candidate text does not match the signed audit "
                f"for {golden_url} / {tool}"
            )
        pair_error = raw_pair.get("error")
        if pair_error is not None:
            if not isinstance(pair_error, str) or not pair_error:
                raise AnalysisError(f"judge pair error is malformed for {golden_url} / {tool}")
            error_count += 1
            continue
        match = raw_pair.get("match")
        confidence = raw_pair.get("confidence")
        if (
            not isinstance(match, bool)
            or not is_finite_number(confidence)
            or not 0 <= confidence <= 1
        ):
            raise AnalysisError(f"judge pair decision is malformed for {golden_url} / {tool}")
        if match and confidence > golden_best_confidence[golden_index]:
            golden_best_confidence[golden_index] = float(confidence)
            golden_matched[golden_index] = True
            candidate_matched[candidate_index] = True
            for sibling in sibling_map.get(candidate_index, set()):
                candidate_matched[sibling] = True
    if coordinates != expected_coordinates:
        raise AnalysisError(f"judge pair matrix is incomplete for {golden_url} / {tool}")
    for candidate_index, item in audit_by_index.items():
        candidate_text = candidate_text_by_index.get(candidate_index)
        if candidate_text is None or sha256_text(candidate_text) != item.get("candidate_sha256"):
            raise AnalysisError(
                f"judged candidate text does not match the signed audit for {golden_url} / {tool}"
            )
    expected_reduction = {
        "tp": sum(golden_matched),
        "fp": sum(not matched for matched in candidate_matched),
        "fn": sum(not matched for matched in golden_matched),
        "errors_count": error_count,
    }
    for key, expected in expected_reduction.items():
        actual = evaluation.get(key)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected:
            raise AnalysisError(
                f"stored judge reduction differs from its pair matrix for {golden_url} / {tool}"
            )
    return tuple(golden_matched)


def _metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(rows)
    tp = sum(int(row.get("tp", 0)) for row in values)
    fp = sum(int(row.get("fp", 0)) for row in values)
    fn = sum(int(row.get("fn", 0)) for row in values)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _percentile(values: Sequence[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = proportion * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_metrics(
    rows: Sequence[Mapping[str, Any]], *, samples: int, seed: int
) -> dict[str, list[float]]:
    if not rows:
        return {"precision": [0.0, 0.0], "recall": [0.0, 0.0], "f1": [0.0, 0.0]}
    rng = random.Random(seed)
    draws: dict[str, list[float]] = defaultdict(list)
    for _ in range(samples):
        metric = _metrics(rows[rng.randrange(len(rows))] for _ in rows)
        for name in ("precision", "recall", "f1"):
            draws[name].append(float(metric[name]))
    return {
        name: [_percentile(values, 0.025), _percentile(values, 0.975)]
        for name, values in draws.items()
    }


def _paired_bootstrap(
    left: Mapping[str, Mapping[str, Any]],
    right: Mapping[str, Mapping[str, Any]],
    *,
    samples: int,
    seed: int,
    allow_case_intersection: bool = False,
) -> dict[str, Any]:
    left_cases = set(left)
    right_cases = set(right)
    case_ids = sorted(left_cases & right_cases)
    if left_cases != right_cases and not allow_case_intersection:
        raise AnalysisError("paired model tracks do not contain the same benchmark cases")
    excluded_cases = sorted(left_cases ^ right_cases)
    case_exclusions = {
        "count": len(excluded_cases),
        "cases": excluded_cases,
        "missing_from_left": sorted(right_cases - left_cases),
        "missing_from_right": sorted(left_cases - right_cases),
    }
    if not case_ids:
        return {
            "case_count": 0,
            "f1_delta": None,
            "f1_delta_95_ci": None,
            "probability_left_gt_right": None,
            "paired_case_exclusions": case_exclusions,
        }
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        draw = [case_ids[rng.randrange(len(case_ids))] for _ in case_ids]
        left_f1 = _metrics(left[case] for case in draw)["f1"]
        right_f1 = _metrics(right[case] for case in draw)["f1"]
        deltas.append(float(left_f1) - float(right_f1))
    # The point estimate and every bootstrap draw must use the same paired
    # population.  Comparing each track's full surviving population here would
    # quietly reintroduce an asymmetric judge-error case after explicitly
    # excluding it from the paired interval.
    point = float(_metrics(left[case] for case in case_ids)["f1"]) - float(
        _metrics(right[case] for case in case_ids)["f1"]
    )
    return {
        "case_count": len(case_ids),
        "f1_delta": point,
        "f1_delta_95_ci": [_percentile(deltas, 0.025), _percentile(deltas, 0.975)],
        "probability_left_gt_right": sum(value > 0 for value in deltas) / len(deltas),
        "paired_case_exclusions": case_exclusions,
    }


def _artifact_stage_counts(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rejected: Counter[str] = Counter()
    context_files: list[int] = []
    generation_tokens: list[int] = []
    verifier_tokens: list[int] = []
    retries = 0
    prompt_bound_cases = 0
    discovery_bound_cases = 0
    index_summary_cases = 0
    discovery_bound_hits: Counter[str] = Counter()
    generation_budget_utilization: list[float] = []
    verifier_budget_utilization: list[float] = []
    for artifact in artifacts:
        for item in artifact.get("rejected_findings", []):
            if isinstance(item, Mapping):
                rejected[str(item.get("stage") or "unknown")] += 1
        context = artifact.get("context", {})
        if isinstance(context, Mapping):
            files = context.get("effective_context_files_exposed_to_model", [])
            context_files.append(len(files) if isinstance(files, list) else 0)
            pressure = context.get("context_pressure", {})
            if isinstance(pressure, Mapping):
                if any(int(pressure.get(name) or 0) > 0 for name in _PROMPT_BOUND_FIELDS):
                    prompt_bound_cases += 1
                generation_budget_utilization.append(
                    float(pressure.get("largest_context_budget_utilization") or 0.0)
                )
                verifier_budget_utilization.append(
                    float(pressure.get("largest_verifier_input_char_budget_utilization") or 0.0)
                )
                raw_hits = pressure.get("selection_bound_hits", {})
                if isinstance(raw_hits, Mapping):
                    per_review_hits = {
                        name: int(raw_hits.get(name) or 0) for name in _DISCOVERY_BOUND_FIELDS
                    }
                    discovery_bound_hits.update(per_review_hits)
                    if any(per_review_hits.values()):
                        discovery_bound_cases += 1
                    if int(raw_hits.get("repository_index_truncated") or 0) > 0:
                        index_summary_cases += 1
        per_stage: dict[str, int] = defaultdict(int)
        for call in artifact.get("calls", []):
            if not isinstance(call, Mapping):
                continue
            per_stage[str(call.get("stage") or "unknown")] += int(call.get("input_tokens") or 0)
            retries += max(0, int(call.get("attempt_count") or 1) - 1)
        for diagnostic in artifact.get("diagnostics", []):
            if isinstance(diagnostic, Mapping) and diagnostic.get("stage") in {
                "verification_semantic_retry",
                "verification",
            }:
                retries += int(
                    diagnostic.get("retry_count") or diagnostic.get("semantic_retry_count") or 0
                )
        generation_tokens.append(per_stage["generation"])
        verifier_tokens.append(per_stage["verification"])
    count = len(artifacts)
    return {
        "reviews": count,
        "raw_findings": sum(len(item.get("raw_findings", [])) for item in artifacts),
        "validated_findings": sum(len(item.get("validated_findings", [])) for item in artifacts),
        "verified_findings": sum(len(item.get("findings", [])) for item in artifacts),
        "rejections_by_stage": dict(sorted(rejected.items())),
        "context_files_mean": mean(context_files) if context_files else 0.0,
        "context_files_max": max(context_files, default=0),
        "generation_input_tokens_mean": mean(generation_tokens) if generation_tokens else 0.0,
        "verifier_input_tokens_mean": mean(verifier_tokens) if verifier_tokens else 0.0,
        "reviews_hitting_prompt_or_evidence_bound": prompt_bound_cases,
        "reviews_hitting_discovery_bound": discovery_bound_cases,
        "reviews_with_hierarchical_index_summary": index_summary_cases,
        "discovery_bound_hits_by_reason": dict(sorted(discovery_bound_hits.items())),
        "generation_budget_utilization_mean": (
            mean(generation_budget_utilization) if generation_budget_utilization else 0.0
        ),
        "generation_budget_utilization_max": max(generation_budget_utilization, default=0.0),
        "verifier_budget_utilization_mean": (
            mean(verifier_budget_utilization) if verifier_budget_utilization else 0.0
        ),
        "verifier_budget_utilization_max": max(verifier_budget_utilization, default=0.0),
        "model_call_retries": retries,
    }


def _decision_by_finding(artifact: Mapping[str, Any]) -> dict[str, tuple[str, float]]:
    final_ids = {
        str(item.get("finding_id"))
        for item in artifact.get("findings", [])
        if isinstance(item, Mapping)
    }
    decisions: dict[str, tuple[str, float]] = {}
    for item in artifact.get("validated_findings", []):
        if not isinstance(item, Mapping):
            continue
        finding_id = str(item.get("finding_id") or "")
        confidence = float(item.get("verifier_confidence") or 0.0)
        if finding_id in final_ids:
            decisions[finding_id] = ("keep", confidence)
    for rejected in artifact.get("rejected_findings", []):
        if not isinstance(rejected, Mapping) or not isinstance(rejected.get("finding"), Mapping):
            continue
        finding = rejected["finding"]
        finding_id = str(finding.get("finding_id") or "")
        stage = str(rejected.get("stage") or "")
        if stage == "verifier_confidence":
            decisions[finding_id] = ("keep", float(finding.get("verifier_confidence") or 0.0))
        elif stage == "semantic_duplicate":
            decisions[finding_id] = (
                "duplicate",
                float(finding.get("verifier_confidence") or 0.0),
            )
        elif stage == "verifier":
            decisions[finding_id] = ("drop", float(finding.get("verifier_confidence") or 0.0))
        elif stage == "verifier_merge":
            decisions[finding_id] = ("merge", float(finding.get("verifier_confidence") or 0.0))
    return decisions


def _threshold_case(
    evaluation: Mapping[str, Any],
    audit: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, tuple[str, float]],
    threshold: float,
) -> dict[str, int]:
    """Re-reduce one judged case at a hypothetical verifier threshold.

    This mirrors the judge's greedy reduction exactly: a pair credits its
    candidate index only when it strictly improves that golden index's best
    confidence. Counting every judged match or keying by repeated text would
    systematically distort false positives relative to the stored reduction.
    """

    selected = {
        int(item["candidate_index"])
        for item in audit
        if isinstance(item.get("candidate_index"), int)
        and decisions.get(str(item.get("finding_id") or ""), ("drop", 0.0))[0] == "keep"
        and decisions.get(str(item.get("finding_id") or ""), ("drop", 0.0))[1] >= threshold
    }
    best_by_golden: dict[int, float] = {}
    matched_candidates: set[int] = set()
    matched_golden: set[int] = set()
    for pair in evaluation.get("pair_matches", []):
        if not isinstance(pair, Mapping) or pair.get("error") or not pair.get("match"):
            continue
        candidate_index = pair.get("candidate_index")
        if candidate_index not in selected:
            continue
        golden_index = pair.get("golden_index")
        if not isinstance(golden_index, int) or isinstance(golden_index, bool):
            continue
        confidence = pair.get("confidence", 0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = 0.0
        if float(confidence) > best_by_golden.get(golden_index, 0.0):
            best_by_golden[golden_index] = float(confidence)
            matched_golden.add(golden_index)
            matched_candidates.add(int(candidate_index))
    total_golden = int(evaluation.get("total_golden", 0))
    return {
        "tp": len(matched_golden),
        "fp": len(selected - matched_candidates),
        "fn": total_golden - len(matched_golden),
    }


def analyze_evaluation(
    *,
    run_dir: Path,
    results_dir: Path,
    judge_model: str,
    output_json: Path,
    bootstrap_samples: int = 2_000,
    bootstrap_seed: int = 17_042,
    allow_judge_errors: bool = False,
) -> dict[str, Any]:
    """Create a reproducible audit report from immutable run/evaluation outputs.

    Judge-skipped rows never enter metrics. Rows whose pair judgments contain
    errors were scored with errored pairs defaulted to non-matches, so pooling
    them silently deflates recall; they fail the analysis unless
    ``allow_judge_errors`` is set, in which case they are excluded and counted.
    """

    run_root = run_dir.expanduser().resolve()
    results_root = results_dir.expanduser().resolve()
    run_manifest_path = run_root / "run_manifest.json"
    judge_root = results_root / sanitize_model_name(judge_model)
    index_path = judge_root / "bugbunny_export_index.json"
    evaluations_path = judge_root / "evaluations.json"
    run_manifest = _object(run_manifest_path)
    index = _object(index_path)
    evaluations = _object(evaluations_path)
    benchmark_data = _object(results_root / "benchmark_data.json")
    all_candidates = _object(judge_root / "candidates.json")
    all_dedup_groups = _object(judge_root / "dedup_groups.json")
    if run_manifest.get("schema_version") != BENCHMARK_RUN_SCHEMA_VERSION:
        raise AnalysisError("run manifest has an unsupported schema")
    if run_manifest.get("implementation") != implementation_identity():
        raise AnalysisError("run manifest belongs to a different BugBunny implementation")
    if index.get("schema_version") != EXPORT_INDEX_SCHEMA_VERSION:
        raise AnalysisError("export index has an unsupported schema")
    if index.get("implementation") != implementation_identity():
        raise AnalysisError("export index belongs to a different BugBunny implementation")
    if index.get("judge_model") != judge_model:
        raise AnalysisError("export index belongs to a different judge model")
    index_output_hashes = index.get("output_files_sha256")
    if not isinstance(index_output_hashes, Mapping):
        raise AnalysisError("export index does not bind the Step 3 output files")
    exports = index.get("exports")
    if not isinstance(exports, list) or not exports:
        raise AnalysisError("export index contains no tracks")

    artifacts_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    artifact_by_model_url: dict[tuple[str, str], dict[str, Any]] = {}
    for record in run_manifest.get("records", []):
        if not isinstance(record, Mapping):
            continue
        model = str(record.get("model") or "")
        case_id = str(record.get("case_id") or "")
        relative = record.get("artifact")
        if not model or not case_id or not isinstance(relative, str):
            raise AnalysisError("run manifest has an incomplete artifact record")
        path = _resolved_child(run_root, relative, label="run artifact path")
        if sha256_bytes(path.read_bytes()) != record.get("artifact_sha256"):
            raise AnalysisError(f"artifact no longer matches run manifest: {path}")
        artifact = _object(path)
        benchmark = artifact.get("benchmark")
        golden_url = benchmark.get("golden_url") if isinstance(benchmark, Mapping) else None
        if not isinstance(golden_url, str) or not golden_url:
            raise AnalysisError(f"artifact has no benchmark golden URL: {path}")
        artifact_key = (model, golden_url)
        if artifact_key in artifact_by_model_url:
            raise AnalysisError(f"run manifest duplicates {model} / {golden_url}")
        artifacts_by_model[model].append(artifact)
        artifact_by_model_url[artifact_key] = artifact

    tracks: dict[str, dict[str, Any]] = {}
    case_rows_by_tool: dict[str, dict[str, Mapping[str, Any]]] = {}
    audit_by_tool: dict[str, dict[str, list[dict[str, Any]]]] = {}
    export_by_model_stage: dict[tuple[str, str], str] = {}
    row_hygiene: dict[str, dict[str, Any]] = {}
    indexed_manifests: set[Path] = set()
    seen_tools: set[str] = set()
    judge_identities: set[str] = set()
    judge_identity_payloads: dict[str, dict[str, Any]] = {}
    category_metrics: dict[str, dict[str, dict[str, int]]] = {}
    for raw_export in exports:
        if not isinstance(raw_export, Mapping):
            raise AnalysisError("export index contains a malformed track")
        tool = str(raw_export.get("tool_id") or "")
        model = str(raw_export.get("model") or "")
        stage = str(raw_export.get("finding_stage") or "balanced")
        audit_relative = raw_export.get("candidate_audit")
        manifest_relative = raw_export.get("manifest")
        manifest_sha256 = raw_export.get("manifest_sha256")
        if (
            not tool
            or tool in seen_tools
            or not model
            or stage not in {"generator", "balanced", "family"}
            or not isinstance(audit_relative, str)
            or not isinstance(manifest_relative, str)
            or not isinstance(manifest_sha256, str)
        ):
            raise AnalysisError("export track is missing or duplicates its immutable identity")
        seen_tools.add(tool)
        model_stage = (model, stage)
        if model_stage in export_by_model_stage:
            raise AnalysisError(f"export index duplicates model/stage {model} / {stage}")

        manifest_path = _resolved_child(
            results_root, manifest_relative, label="export manifest path"
        )
        if manifest_path.parent != judge_root or manifest_path in indexed_manifests:
            raise AnalysisError("export index references a misplaced or duplicate manifest")
        indexed_manifests.add(manifest_path)
        if (
            not manifest_path.is_file()
            or sha256_bytes(manifest_path.read_bytes()) != manifest_sha256
        ):
            raise AnalysisError(f"export manifest no longer matches its index: {manifest_path}")
        manifest_payload = _object(manifest_path)
        try:
            verified = verify_codereviewbench_export_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            raise AnalysisError(f"export manifest verification failed for {tool}: {exc}") from exc
        if (
            verified.get("tool_id") != tool
            or int(verified.get("review_count") or 0) != raw_export.get("reviews")
            or int(verified.get("candidate_count") or 0) != raw_export.get("candidates")
            or dict(verified.get("output_files_sha256") or {}) != dict(index_output_hashes)
            or manifest_payload.get("review_model") != model
            or manifest_payload.get("finding_stage") != stage
        ):
            raise AnalysisError(f"export index metadata differs from manifest for {tool}")

        audit_path = _resolved_child(results_root, audit_relative, label="candidate audit path")
        audit_name = manifest_payload.get("candidate_audit_file")
        if not isinstance(audit_name, str) or audit_path != manifest_path.parent / audit_name:
            raise AnalysisError(f"export index points at the wrong candidate audit for {tool}")
        audit_payload = _object(audit_path)
        cases = audit_payload.get("cases")
        if (
            audit_payload.get("schema_version") != CANDIDATE_AUDIT_SCHEMA_VERSION
            or audit_payload.get("implementation") != implementation_identity()
            or audit_payload.get("tool_id") != tool
            or audit_payload.get("review_model") != model
            or audit_payload.get("finding_stage") != stage
            or not isinstance(cases, Mapping)
        ):
            raise AnalysisError(f"candidate audit has no cases: {audit_relative}")
        case_audits: dict[str, list[dict[str, Any]]] = {}
        for url, values in cases.items():
            if (
                not isinstance(url, str)
                or not isinstance(values, list)
                or any(not isinstance(item, Mapping) for item in values)
            ):
                raise AnalysisError(f"candidate audit has malformed rows: {audit_relative}")
            case_audits[url] = [dict(item) for item in values]
        audit_by_tool[tool] = case_audits
        category_metrics.setdefault(tool, {})
        expected_cases = set(case_audits)
        artifact_cases = {
            url for artifact_model, url in artifact_by_model_url if artifact_model == model
        }
        if expected_cases != artifact_cases:
            raise AnalysisError(f"export and run artifact case populations differ for {tool}")
        artifact_hashes = manifest_payload.get("artifact_canonical_sha256")
        if not isinstance(artifact_hashes, Mapping) or set(artifact_hashes) != expected_cases:
            raise AnalysisError(f"export manifest has incomplete artifact identity for {tool}")
        for url in sorted(expected_cases):
            artifact = artifact_by_model_url[(model, url)]
            if sha256_text(canonical_json(artifact)) != artifact_hashes.get(url):
                raise AnalysisError(
                    f"run artifact no longer matches the exported artifact identity for {url} / {tool}"
                )

        present_cases: set[str] = set()
        evaluation_by_case: dict[str, Mapping[str, Any]] = {}
        for url, per_tool in evaluations.items():
            if (
                not isinstance(url, str)
                or not isinstance(per_tool, Mapping)
                or tool not in per_tool
            ):
                continue
            result = per_tool[tool]
            if not isinstance(result, Mapping):
                raise AnalysisError(f"evaluation row is malformed for {url} / {tool}")
            present_cases.add(url)
            evaluation_by_case[url] = result
        if present_cases != expected_cases:
            missing = sorted(expected_cases - present_cases)
            extra = sorted(present_cases - expected_cases)
            raise AnalysisError(
                f"evaluation case population differs for {tool}; missing={missing}, extra={extra}"
            )

        rows: dict[str, Mapping[str, Any]] = {}
        skipped_rows = 0
        error_rows: list[str] = []
        skipped_cases: list[str] = []
        for url in sorted(expected_cases):
            result = evaluation_by_case[url]
            benchmark_entry = benchmark_data.get(url)
            per_case_candidates = all_candidates.get(url)
            per_case_groups = all_dedup_groups.get(url)
            raw_goldens = (
                benchmark_entry.get("golden_comments")
                if isinstance(benchmark_entry, Mapping)
                else None
            )
            raw_candidates = (
                per_case_candidates.get(tool) if isinstance(per_case_candidates, Mapping) else None
            )
            groups = per_case_groups.get(tool) if isinstance(per_case_groups, Mapping) else None
            if (
                not isinstance(raw_goldens, list)
                or any(not isinstance(item, Mapping) for item in raw_goldens)
                or not isinstance(raw_candidates, list)
                or any(
                    not isinstance(item, Mapping) or not isinstance(item.get("text"), str)
                    for item in raw_candidates
                )
                or (groups is not None and not isinstance(groups, list))
            ):
                raise AnalysisError(f"current judge inputs are malformed for {url} / {tool}")
            golden_comments = [dict(item) for item in raw_goldens]
            candidate_texts = [str(item["text"]) for item in raw_candidates]
            try:
                identity_payload = validate_judge_identity_payload(
                    result.get("judge_identity"), expected_model=judge_model
                )
            except ValueError as exc:
                raise AnalysisError(
                    f"evaluation has no verifiable current judge identity for {url} / {tool}: {exc}"
                ) from exc
            judge_identity = sha256_text(canonical_json(identity_payload))
            if (
                result.get("judge_identity_version") != JUDGE_IDENTITY_VERSION
                or result.get("judge_identity_sha256") != judge_identity
            ):
                raise AnalysisError(f"evaluation judge identity hash differs for {url} / {tool}")
            judge_identities.add(judge_identity)
            judge_identity_payloads[judge_identity] = identity_payload
            expected_judged_inputs = judged_inputs_sha256(
                golden_comments,
                candidate_texts,
                groups,
                judge_identity=judge_identity,
            )
            if result.get("judged_inputs_sha256") != expected_judged_inputs:
                raise AnalysisError(f"evaluation judged-input identity differs for {url} / {tool}")
            matched_goldens = _bind_judged_candidate_inputs(
                tool=tool,
                golden_url=url,
                evaluation=result,
                audit=case_audits[url],
                golden_comments=golden_comments,
                candidates=candidate_texts,
                dedup_groups=groups,
            )
            artifact = artifact_by_model_url[(model, url)]
            artifact_finding_ids = {
                str(item.get("finding_id"))
                for stream in ("validated_findings", "findings")
                for item in artifact.get(stream, [])
                if isinstance(item, Mapping) and item.get("finding_id")
            }
            foreign = [
                str(item.get("finding_id"))
                for item in case_audits[url]
                if str(item.get("finding_id") or "") not in artifact_finding_ids
            ]
            if foreign:
                raise AnalysisError(
                    f"candidate audit for {url} references findings absent "
                    f"from the run artifact: {', '.join(sorted(foreign)[:5])}"
                )
            # Mirror the judge's own aggregation hygiene: skipped rows carry no
            # judgments, and error-degraded rows scored failed pairs as
            # non-matches, so neither may enter published metrics.
            if result.get("skipped"):
                skipped_rows += 1
                skipped_cases.append(url)
                continue
            if int(result.get("errors_count") or 0) > 0:
                error_rows.append(url)
                continue
            rows[url] = result
            if matched_goldens is None:
                raise AnalysisError(f"clean evaluation unexpectedly has no pair matrix: {url}")
            tool_categories = category_metrics.setdefault(tool, {})
            for golden_index, matched in enumerate(matched_goldens):
                category = str(golden_comments[golden_index].get("category") or "unknown")
                counts = tool_categories.setdefault(category, {"tp": 0, "fn": 0})
                counts["tp" if matched else "fn"] += 1
        if error_rows and not allow_judge_errors:
            raise AnalysisError(
                f"tool {tool} has judge-error-degraded evaluations for "
                f"{', '.join(sorted(error_rows))}; re-judge them or pass "
                "allow_judge_errors to exclude them explicitly"
            )
        row_hygiene[tool] = {
            "rows_expected": len(expected_cases),
            "rows_used": len(rows),
            "rows_skipped_excluded": skipped_rows,
            "skipped_excluded_cases": skipped_cases,
            "rows_error_excluded": len(error_rows),
            "error_excluded_cases": sorted(error_rows),
        }
        case_rows_by_tool[tool] = rows
        metric = _metrics(rows.values())
        metric["bootstrap_95_ci"] = _bootstrap_metrics(
            list(rows.values()), samples=bootstrap_samples, seed=bootstrap_seed
        )
        metric["reviews"] = len(rows)
        metric["exported_candidates"] = int(raw_export.get("candidates") or 0)
        tracks[tool] = {"model": model, "finding_stage": stage, "metrics": metric}
        export_by_model_stage[model_stage] = tool

    committed_manifests = {path.resolve() for path in judge_root.glob("*_export_manifest.json")}
    if indexed_manifests != committed_manifests:
        missing = sorted(str(path) for path in committed_manifests - indexed_manifests)
        extra = sorted(str(path) for path in indexed_manifests - committed_manifests)
        raise AnalysisError(
            "export index does not enumerate the committed manifest set; "
            f"unindexed={missing}, missing={extra}"
        )
    if len(judge_identities) != 1:
        raise AnalysisError(
            "analysis inputs contain multiple judge identities; rejudge every compared tool "
            "under one exact model/backend/prompt/timeout/retry contract"
        )
    common_judge_identity = next(iter(judge_identities))
    common_judge_identity_payload = judge_identity_payloads[common_judge_identity]

    pairwise: dict[str, Any] = {}
    by_stage: dict[str, list[str]] = defaultdict(list)
    for tool, track in tracks.items():
        by_stage[track["finding_stage"]].append(tool)
    for stage, tools in sorted(by_stage.items()):
        for left_index, left in enumerate(sorted(tools)):
            for right in sorted(tools)[left_index + 1 :]:
                key = f"{left}__minus__{right}"
                pairwise[key] = {
                    "finding_stage": stage,
                    **_paired_bootstrap(
                        case_rows_by_tool[left],
                        case_rows_by_tool[right],
                        samples=bootstrap_samples,
                        seed=bootstrap_seed,
                        allow_case_intersection=allow_judge_errors,
                    ),
                }

    stage_counts = {
        model: _artifact_stage_counts(values)
        for model, values in sorted(artifacts_by_model.items())
    }
    threshold_curves: dict[str, list[dict[str, Any]]] = {}
    for model in sorted(artifacts_by_model):
        generator_tool = export_by_model_stage.get((model, "generator"))
        if generator_tool is None:
            continue
        case_artifacts = {
            str(item.get("benchmark", {}).get("golden_url")): item
            for item in artifacts_by_model[model]
            if isinstance(item.get("benchmark"), Mapping)
        }
        thresholds = {round(index / 20, 2) for index in range(21)}
        thresholds.update(
            float(item.get("config", {}).get("min_verifier_confidence"))
            for item in artifacts_by_model[model]
            if isinstance(item.get("config"), Mapping)
            and isinstance(item["config"].get("min_verifier_confidence"), (int, float))
        )
        curve: list[dict[str, Any]] = []
        for threshold in sorted(thresholds):
            rows = []
            for golden_url, evaluation in case_rows_by_tool[generator_tool].items():
                rows.append(
                    _threshold_case(
                        evaluation,
                        audit_by_tool[generator_tool].get(golden_url, []),
                        _decision_by_finding(case_artifacts[golden_url]),
                        threshold,
                    )
                )
            curve.append({"threshold": threshold, **_metrics(rows)})
        threshold_curves[model] = curve

    report = {
        "schema_version": EVALUATION_AUDIT_SCHEMA_VERSION,
        "implementation": implementation_identity(),
        "created_at": utc_now(),
        "inputs": {
            "run_manifest": str(run_manifest_path),
            "run_manifest_sha256": sha256_bytes(run_manifest_path.read_bytes()),
            "export_index": str(index_path),
            "export_index_sha256": sha256_bytes(index_path.read_bytes()),
            "evaluations": str(evaluations_path),
            "evaluations_sha256": sha256_bytes(evaluations_path.read_bytes()),
            "judge_model": judge_model,
            "judge_identity": common_judge_identity_payload,
            "judge_identity_sha256": common_judge_identity,
        },
        "bootstrap": {"samples": bootstrap_samples, "seed": bootstrap_seed, "unit": "pull_request"},
        "judge_row_hygiene": dict(sorted(row_hygiene.items())),
        "stage_counts": stage_counts,
        "tracks": dict(sorted(tracks.items())),
        "paired_model_comparisons": pairwise,
        "threshold_curves_from_generator_judgments": threshold_curves,
        "golden_category_counts": category_metrics,
        "interpretation_limits": [
            "CodeReviewBench golden comments may be incomplete, so false positives are benchmark-relative.",
            "Threshold curves reuse the fixed judge pair matrix; they do not make additional judge calls.",
            "Confidence intervals resample pull requests and do not model judge-model uncertainty.",
            "Hierarchical repository-index summarization is reported separately from prompt and discovery bounds because the full inventory remains pageable.",
        ],
    }
    atomic_write_json(output_json, report)
    return report


def render_analysis_markdown(report: Mapping[str, Any]) -> str:
    lines = ["# BugBunny evaluation audit", "", "## Tracks", ""]
    lines.append("| Model | Stage | Candidates | Precision | Recall | F1 | F1 95% CI |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for track in report.get("tracks", {}).values():
        metric = track["metrics"]
        interval = metric["bootstrap_95_ci"]["f1"]
        lines.append(
            f"| {track['model']} | {track['finding_stage']} | {metric['exported_candidates']} "
            f"| {metric['precision']:.3f} | {metric['recall']:.3f} | {metric['f1']:.3f} "
            f"| [{interval[0]:.3f}, {interval[1]:.3f}] |"
        )
    lines.extend(["", "## Pipeline counts", ""])
    lines.append(
        "| Model | Raw | Validated | Verified | Prompt-bound | Discovery-bound "
        "| Index-summarized | Gen budget max | Verifier budget max | Retries |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for model, counts in report.get("stage_counts", {}).items():
        lines.append(
            f"| {model} | {counts['raw_findings']} | {counts['validated_findings']} "
            f"| {counts['verified_findings']} "
            f"| {counts['reviews_hitting_prompt_or_evidence_bound']} "
            f"| {counts['reviews_hitting_discovery_bound']} "
            f"| {counts['reviews_with_hierarchical_index_summary']} "
            f"| {counts['generation_budget_utilization_max']:.3f} "
            f"| {counts['verifier_budget_utilization_max']:.3f} "
            f"| {counts['model_call_retries']} |"
        )
    lines.extend(["", "## Interpretation limits", ""])
    lines.extend(f"- {value}" for value in report.get("interpretation_limits", []))
    return "\n".join(lines) + "\n"


__all__ = ["AnalysisError", "analyze_evaluation", "render_analysis_markdown"]
