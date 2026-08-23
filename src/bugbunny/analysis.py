"""Post-hoc evaluation diagnostics that never change benchmark scoring."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean
from typing import Any

from bugbunny.benchmark import sanitize_model_name
from bugbunny.util import atomic_write_json, load_json, sha256_bytes, utc_now


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
) -> dict[str, Any]:
    case_ids = sorted(set(left) & set(right))
    if set(left) != set(right):
        raise AnalysisError("paired model tracks do not contain the same benchmark cases")
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        draw = [case_ids[rng.randrange(len(case_ids))] for _ in case_ids]
        left_f1 = _metrics(left[case] for case in draw)["f1"]
        right_f1 = _metrics(right[case] for case in draw)["f1"]
        deltas.append(float(left_f1) - float(right_f1))
    point = float(_metrics(left.values())["f1"]) - float(_metrics(right.values())["f1"])
    return {
        "case_count": len(case_ids),
        "f1_delta": point,
        "f1_delta_95_ci": [_percentile(deltas, 0.025), _percentile(deltas, 0.975)],
        "probability_left_gt_right": sum(value > 0 for value in deltas) / len(deltas),
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
                    float(
                        pressure.get("largest_verifier_input_char_budget_utilization") or 0.0
                    )
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
                    diagnostic.get("retry_count")
                    or diagnostic.get("semantic_retry_count")
                    or 0
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
    selected = {
        int(item["candidate_index"])
        for item in audit
        if isinstance(item.get("candidate_index"), int)
        and decisions.get(str(item.get("finding_id") or ""), ("drop", 0.0))[0] == "keep"
        and decisions.get(str(item.get("finding_id") or ""), ("drop", 0.0))[1] >= threshold
    }
    matched_candidates: set[int] = set()
    matched_golden: set[int] = set()
    for pair in evaluation.get("pair_matches", []):
        if not isinstance(pair, Mapping) or pair.get("error") or not pair.get("match"):
            continue
        candidate_index = pair.get("candidate_index")
        golden_index = pair.get("golden_index")
        if candidate_index in selected and isinstance(golden_index, int):
            matched_candidates.add(int(candidate_index))
            matched_golden.add(golden_index)
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
) -> dict[str, Any]:
    """Create a reproducible audit report from immutable run/evaluation outputs."""

    run_root = run_dir.expanduser().resolve()
    results_root = results_dir.expanduser().resolve()
    run_manifest_path = run_root / "run_manifest.json"
    judge_root = results_root / sanitize_model_name(judge_model)
    index_path = judge_root / "bugbunny_export_index.json"
    evaluations_path = judge_root / "evaluations.json"
    run_manifest = _object(run_manifest_path)
    index = _object(index_path)
    evaluations = _object(evaluations_path)
    exports = index.get("exports")
    if not isinstance(exports, list) or not exports:
        raise AnalysisError("export index contains no tracks")

    artifacts_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    artifact_by_model_case: dict[tuple[str, str], dict[str, Any]] = {}
    for record in run_manifest.get("records", []):
        if not isinstance(record, Mapping):
            continue
        model = str(record.get("model") or "")
        case_id = str(record.get("case_id") or "")
        relative = record.get("artifact")
        if not model or not case_id or not isinstance(relative, str):
            raise AnalysisError("run manifest has an incomplete artifact record")
        path = run_root / relative
        if sha256_bytes(path.read_bytes()) != record.get("artifact_sha256"):
            raise AnalysisError(f"artifact no longer matches run manifest: {path}")
        artifact = _object(path)
        artifacts_by_model[model].append(artifact)
        artifact_by_model_case[(model, case_id)] = artifact

    tracks: dict[str, dict[str, Any]] = {}
    case_rows_by_tool: dict[str, dict[str, Mapping[str, Any]]] = {}
    audit_by_tool: dict[str, dict[str, list[dict[str, Any]]]] = {}
    export_by_model_stage: dict[tuple[str, str], str] = {}
    for raw_export in exports:
        if not isinstance(raw_export, Mapping):
            raise AnalysisError("export index contains a malformed track")
        tool = str(raw_export.get("tool_id") or "")
        model = str(raw_export.get("model") or "")
        stage = str(raw_export.get("finding_stage") or "balanced")
        audit_relative = raw_export.get("candidate_audit")
        if not tool or not model or not isinstance(audit_relative, str):
            raise AnalysisError("export track is missing model, tool, or candidate audit")
        audit_payload = _object(results_root / audit_relative)
        cases = audit_payload.get("cases")
        if not isinstance(cases, Mapping):
            raise AnalysisError(f"candidate audit has no cases: {audit_relative}")
        audit_by_tool[tool] = {
            str(url): [dict(item) for item in values if isinstance(item, Mapping)]
            for url, values in cases.items()
            if isinstance(values, list)
        }
        rows = {
            str(url): per_tool[tool]
            for url, per_tool in evaluations.items()
            if isinstance(per_tool, Mapping)
            and isinstance(per_tool.get(tool), Mapping)
        }
        case_rows_by_tool[tool] = rows
        metric = _metrics(rows.values())
        metric["bootstrap_95_ci"] = _bootstrap_metrics(
            list(rows.values()), samples=bootstrap_samples, seed=bootstrap_seed
        )
        metric["reviews"] = len(rows)
        metric["exported_candidates"] = int(raw_export.get("candidates") or 0)
        tracks[tool] = {"model": model, "finding_stage": stage, "metrics": metric}
        export_by_model_stage[(model, stage)] = tool

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
                    ),
                }

    stage_counts = {
        model: _artifact_stage_counts(values) for model, values in sorted(artifacts_by_model.items())
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
                artifact = case_artifacts.get(golden_url)
                if artifact is None:
                    raise AnalysisError("generator evaluation has no matching run artifact")
                rows.append(
                    _threshold_case(
                        evaluation,
                        audit_by_tool[generator_tool].get(golden_url, []),
                        _decision_by_finding(artifact),
                        threshold,
                    )
                )
            curve.append({"threshold": threshold, **_metrics(rows)})
        threshold_curves[model] = curve

    category_metrics: dict[str, dict[str, dict[str, int]]] = {}
    for tool, rows in case_rows_by_tool.items():
        categories: dict[str, Counter[str]] = defaultdict(Counter)
        for result in rows.values():
            for item in result.get("true_positives", []):
                if isinstance(item, Mapping):
                    categories[str(item.get("category") or "unknown")]["tp"] += 1
            for item in result.get("false_negatives", []):
                if isinstance(item, Mapping):
                    categories[str(item.get("category") or "unknown")]["fn"] += 1
        category_metrics[tool] = {
            category: dict(counter) for category, counter in sorted(categories.items())
        }

    report = {
        "schema_version": "bugbunny-evaluation-audit-v2",
        "created_at": utc_now(),
        "inputs": {
            "run_manifest": str(run_manifest_path),
            "run_manifest_sha256": sha256_bytes(run_manifest_path.read_bytes()),
            "export_index": str(index_path),
            "export_index_sha256": sha256_bytes(index_path.read_bytes()),
            "evaluations": str(evaluations_path),
            "evaluations_sha256": sha256_bytes(evaluations_path.read_bytes()),
            "judge_model": judge_model,
        },
        "bootstrap": {"samples": bootstrap_samples, "seed": bootstrap_seed, "unit": "pull_request"},
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
