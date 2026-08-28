"""External verifier calibration with a sealed, non-benchmark corpus."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bugbunny.gateway import GatewayError, ModelGateway
from bugbunny.prompts import (
    VERIFIER_PROMPT_VERSION,
    build_verifier_prompt,
    verifier_prompt_sha256,
)
from bugbunny.schemas import VERIFIER_SCHEMA, findings_from_payload, validate_verifier_payload
from bugbunny.util import atomic_write_json, canonical_json, sha256_bytes, sha256_text, utc_now


class CalibrationError(ValueError):
    """A calibration corpus, run, or frozen operating point is invalid."""


def _load_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw = path.expanduser().resolve().read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"invalid calibration JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CalibrationError("calibration JSON must contain an object")
    return raw, value


def load_calibration_corpus(path: Path) -> tuple[dict[str, Any], str]:
    raw, value = _load_object(path)
    if value.get("schema_version") != "bugbunny-verifier-calibration-corpus-v1":
        raise CalibrationError("unsupported verifier calibration corpus")
    provenance = value.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("contains_codereviewbench") is not False
    ):
        raise CalibrationError("corpus must attest that it excludes CodeReviewBench cases")
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) < 20:
        raise CalibrationError("calibration corpus must contain at least 20 cases")
    seen: set[str] = set()
    labels: set[bool] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise CalibrationError(f"calibration case {index} is not an object")
        case_id = case.get("case_id")
        label = case.get("valid_candidate")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise CalibrationError(f"calibration case {index} has an invalid/duplicate ID")
        if not isinstance(label, bool):
            raise CalibrationError(f"calibration case {case_id} has no boolean label")
        if not all(isinstance(case.get(field), str) for field in ("patch", "context", "rationale")):
            raise CalibrationError(f"calibration case {case_id} lacks evidence or rationale")
        finding_payload = case.get("finding")
        if not isinstance(finding_payload, Mapping):
            raise CalibrationError(f"calibration case {case_id} has no finding")
        findings_from_payload({"findings": [dict(finding_payload)]}, chunk_id=case_id)
        seen.add(case_id)
        labels.add(label)
    if labels != {False, True}:
        raise CalibrationError("calibration corpus must contain positive and negative labels")
    return value, sha256_bytes(raw)


def select_operating_point(
    observations: list[Mapping[str, Any]],
    *,
    minimum_precision: float = 0.80,
) -> dict[str, Any]:
    """Select the conservative edge of the best precision-constrained plateau."""

    if not 0 <= minimum_precision <= 1:
        raise CalibrationError("minimum_precision must be between 0 and 1")
    if not observations:
        raise CalibrationError("cannot calibrate without observations")
    candidates = {0.0, 1.0}
    for item in observations:
        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)) and math.isfinite(float(confidence)):
            # The exact observed value: rounding here would create thresholds
            # that sit on the wrong side of the >= comparison below.
            candidates.add(float(confidence))
    rows: list[dict[str, Any]] = []
    for threshold in sorted(candidates):
        tp = fp = fn = 0
        for item in observations:
            expected = item.get("valid_candidate") is True
            predicted = (
                item.get("decision") == "keep" and float(item.get("confidence") or 0.0) >= threshold
            )
            tp += int(expected and predicted)
            fp += int(not expected and predicted)
            fn += int(expected and not predicted)
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        rows.append(
            {
                "threshold": threshold,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    # A threshold that keeps nothing has vacuous precision; letting it satisfy
    # the floor would silently freeze a recall-0 operating point whenever no
    # real threshold qualifies, instead of failing loudly below.
    eligible = [
        row for row in rows if row["tp"] + row["fp"] > 0 and row["precision"] >= minimum_precision
    ]
    if not eligible:
        raise CalibrationError("no threshold satisfies the required calibration precision")
    # Recall is the first-order objective under a precision floor. F1 breaks
    # ties; the highest threshold on an identical observed plateau is the
    # conservative choice for unseen, lower-confidence candidates.
    selected = max(eligible, key=lambda row: (row["recall"], row["f1"], row["threshold"]))
    # Diagnostics only: the selection itself is unchanged and the fields
    # below are deliberately kept out of `selected` so archived operating
    # points still re-derive byte-for-byte. They exist because a small or
    # perfectly separated corpus makes the frozen threshold a tie-break
    # artifact and the precision floor statistically unverifiable, and that
    # must be visible rather than implied by a nominal 1.0.
    predicted_positives = int(selected["tp"] + selected["fp"])
    precision_lower_bound = _clopper_pearson_lower(int(selected["tp"]), predicted_positives)
    plateau = [
        row["threshold"]
        for row in eligible
        if row["recall"] == selected["recall"] and row["f1"] == selected["f1"]
    ]
    return {
        "objective": "maximize_recall_subject_to_precision_floor_then_f1",
        "minimum_precision": minimum_precision,
        "selected": selected,
        "curve": rows,
        "uncertainty": {
            "predicted_positives": predicted_positives,
            "precision_95_lower_bound": precision_lower_bound,
            "precision_floor_statistically_verified": precision_lower_bound >= minimum_precision,
            "selected_plateau_thresholds": sorted(plateau),
            # Perfect separation at the selected point: every threshold on
            # the plateau scores identically, so the frozen value is chosen
            # by tie-break rather than by an observed trade-off.
            "corpus_saturated": selected["precision"] == 1.0 and selected["recall"] == 1.0,
        },
    }


def _clopper_pearson_lower(successes: int, trials: int, *, alpha: float = 0.05) -> float:
    """Lower limit of the exact two-sided 95% Clopper-Pearson interval."""

    if trials <= 0 or successes <= 0:
        return 0.0

    def tail_at_least(probability: float) -> float:
        return sum(
            math.comb(trials, count) * probability**count * (1 - probability) ** (trials - count)
            for count in range(successes, trials + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(60):
        middle = (low + high) / 2
        if tail_at_least(middle) < alpha / 2:
            low = middle
        else:
            high = middle
    return low


def verify_corpus_benchmark_disjoint(
    corpus_path: Path, benchmark_data_path: Path
) -> dict[str, Any]:
    """Cross-check the corpus's self-declared attestation against real data.

    ``contains_codereviewbench: false`` is otherwise an honor-system boolean;
    nothing compared the corpus against the 50 cases available locally. This
    check fails when any corpus case textually contains a golden PR URL or a
    golden comment, the direct derivation vectors.
    """

    corpus, corpus_sha256 = load_calibration_corpus(corpus_path)
    _, benchmark = _load_object(Path(benchmark_data_path))
    golden_urls = {str(url).strip().lower() for url in benchmark if str(url).strip()}
    golden_comments: set[str] = set()
    for entry in benchmark.values():
        if isinstance(entry, Mapping):
            for comment in entry.get("golden_comments", []):
                if isinstance(comment, Mapping):
                    text = str(comment.get("comment") or "").strip().lower()
                    # Very short phrases would match incidentally.
                    if len(text) >= 24:
                        golden_comments.add(text)
    overlaps: list[dict[str, str]] = []
    for case in corpus["cases"]:
        parts = [
            str(case.get(field) or "") for field in ("case_id", "patch", "context", "rationale")
        ]
        finding = case.get("finding")
        if isinstance(finding, Mapping):
            parts.extend(str(value) for value in finding.values())
        haystack = "\n".join(parts).lower()
        for url in golden_urls:
            if url in haystack:
                overlaps.append(
                    {"case_id": str(case["case_id"]), "kind": "golden_url", "value": url}
                )
        for text in golden_comments:
            if text in haystack:
                overlaps.append(
                    {"case_id": str(case["case_id"]), "kind": "golden_comment", "value": text}
                )
    if overlaps:
        detail = "; ".join(f"{item['case_id']} contains a {item['kind']}" for item in overlaps[:5])
        raise CalibrationError(f"calibration corpus overlaps the CodeReviewBench dataset: {detail}")
    return {
        "corpus_sha256": corpus_sha256,
        "benchmark_cases": len(benchmark),
        "checked_corpus_cases": len(corpus["cases"]),
        "golden_urls_checked": len(golden_urls),
        "golden_comments_checked": len(golden_comments),
        "overlaps": [],
    }


async def calibrate_verifier(
    *,
    corpus_path: Path,
    output_path: Path,
    gateway: ModelGateway,
    verifier_model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    concurrency: int = 8,
    minimum_precision: float = 0.80,
) -> dict[str, Any]:
    """Run the pinned verifier once per external case and freeze its threshold."""

    corpus, corpus_sha256 = load_calibration_corpus(corpus_path)
    if concurrency <= 0:
        raise CalibrationError("calibration concurrency must be positive")
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(case: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(case["case_id"])
        finding = findings_from_payload({"findings": [dict(case["finding"])]}, chunk_id=case_id)[0]
        prompt = build_verifier_prompt(
            [finding],
            str(case["patch"]),
            str(case["context"]),
            max_batch_size=1,
        )
        try:
            async with semaphore:
                result = await gateway.complete_json(
                    prompt,
                    model=verifier_model,
                    stage="calibration_verification",
                    schema_name="bugbunny_verification",
                    schema=VERIFIER_SCHEMA,
                    chunk_id=case_id,
                    reasoning_effort=reasoning_effort,
                    max_output_tokens=max_output_tokens,
                )
        except GatewayError as exc:
            raise CalibrationError(f"verifier calibration failed for {case_id}: {exc}") from exc
        decision = validate_verifier_payload(result.payload, candidate_count=1)[0]
        return {
            "case_id": case_id,
            "valid_candidate": bool(case["valid_candidate"]),
            "decision": decision["decision"],
            "confidence": decision["confidence"],
            "reason": decision["reason"],
            "family_key": decision["family_key"],
            "call": result.call.to_dict(),
        }

    observations = list(await asyncio.gather(*(evaluate(case) for case in corpus["cases"])))
    observations.sort(key=lambda item: item["case_id"])
    selection = select_operating_point(observations, minimum_precision=minimum_precision)
    observation_sha256 = sha256_text(canonical_json(observations))
    identity = sha256_text(
        canonical_json(
            {
                "corpus_sha256": corpus_sha256,
                "verifier_model": verifier_model,
                "reasoning_effort": reasoning_effort,
                "verifier_prompt_sha256": verifier_prompt_sha256(),
                "observation_sha256": observation_sha256,
                "selection": selection["selected"],
            }
        )
    )
    operating_point = {
        "schema_version": "bugbunny-verifier-operating-point-v1",
        "created_at": utc_now(),
        "operating_point_id": f"bugbunny-op-{identity[:16]}",
        "verifier_model": verifier_model,
        "reasoning_effort": reasoning_effort,
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "verifier_prompt_sha256": verifier_prompt_sha256(),
        "verifier_schema_sha256": sha256_text(canonical_json(VERIFIER_SCHEMA)),
        "corpus": {
            "path": corpus_path.name,
            "sha256": corpus_sha256,
            "case_count": len(observations),
            "contains_codereviewbench": False,
        },
        "observation_sha256": observation_sha256,
        "observations": observations,
        "selection": selection,
        "threshold": selection["selected"]["threshold"],
    }
    atomic_write_json(output_path, operating_point)
    return operating_point


def load_operating_point(path: Path) -> tuple[dict[str, Any], str]:
    raw, value = _load_object(path)
    if value.get("schema_version") != "bugbunny-verifier-operating-point-v1":
        raise CalibrationError("unsupported verifier operating point")
    required_strings = (
        "operating_point_id",
        "verifier_model",
        "reasoning_effort",
        "verifier_prompt_version",
        "verifier_prompt_sha256",
        "observation_sha256",
    )
    if any(not isinstance(value.get(field), str) or not value[field] for field in required_strings):
        raise CalibrationError("operating point is missing required identity fields")
    threshold = value.get("threshold")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
        or not 0 <= float(threshold) <= 1
    ):
        raise CalibrationError("operating point threshold is outside [0,1]")
    corpus = value.get("corpus")
    if not isinstance(corpus, Mapping) or corpus.get("contains_codereviewbench") is not False:
        raise CalibrationError("operating point is not bound to an external corpus")
    observations = value.get("observations")
    if not isinstance(observations, list) or sha256_text(canonical_json(observations)) != value.get(
        "observation_sha256"
    ):
        raise CalibrationError("operating point observations do not match their hash")
    if value["verifier_prompt_sha256"] != verifier_prompt_sha256():
        raise CalibrationError("operating point was produced with a different verifier prompt")
    if value.get("verifier_schema_sha256") != sha256_text(canonical_json(VERIFIER_SCHEMA)):
        raise CalibrationError("operating point was produced with a different verifier schema")

    # The threshold is the operative number, so hashing the observations is not
    # enough: rebind it by re-deriving the selection from those observations
    # and by recomputing the identity embedded in operating_point_id. A file
    # whose threshold no longer derives from its own labeled responses fails
    # closed here instead of silently changing the balanced stage.
    selection = value.get("selection")
    if not isinstance(selection, Mapping) or not isinstance(selection.get("selected"), Mapping):
        raise CalibrationError("operating point has no bound selection record")
    selection_floor = selection.get("minimum_precision")
    if (
        not isinstance(selection_floor, (int, float))
        or isinstance(selection_floor, bool)
        or not 0 <= float(selection_floor) <= 1
    ):
        raise CalibrationError("operating point selection has no valid minimum_precision")
    rederived = select_operating_point(
        [item for item in observations if isinstance(item, Mapping)],
        minimum_precision=float(selection_floor),
    )["selected"]
    if dict(selection["selected"]) != rederived or float(value["threshold"]) != float(
        rederived["threshold"]
    ):
        raise CalibrationError(
            "operating point threshold does not derive from its bound observations"
        )
    corpus_sha256 = corpus.get("sha256")
    if not isinstance(corpus_sha256, str) or not corpus_sha256:
        raise CalibrationError("operating point corpus hash is missing")
    identity = sha256_text(
        canonical_json(
            {
                "corpus_sha256": corpus_sha256,
                "verifier_model": value["verifier_model"],
                "reasoning_effort": value["reasoning_effort"],
                "verifier_prompt_sha256": value["verifier_prompt_sha256"],
                "observation_sha256": value["observation_sha256"],
                "selection": rederived,
            }
        )
    )
    if value["operating_point_id"] != f"bugbunny-op-{identity[:16]}":
        raise CalibrationError("operating point identity does not match its bound contents")
    return value, sha256_bytes(raw)


__all__ = [
    "CalibrationError",
    "calibrate_verifier",
    "load_calibration_corpus",
    "load_operating_point",
    "select_operating_point",
    "verify_corpus_benchmark_disjoint",
]
