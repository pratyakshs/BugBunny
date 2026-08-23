from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from bugbunny.models import Finding

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = (
    "bug",
    "security",
    "concurrency",
    "data",
    "api",
    "performance",
    "test_gap",
    "doc_defect",
    "style",
    "speculative",
)
CATEGORY_ALIASES = {
    "correctness": "bug",
    "functional": "bug",
    "logic": "bug",
    "logic_error": "bug",
    "reliability": "bug",
    "auth": "security",
    "authentication": "security",
    "authorization": "security",
    "privacy": "security",
    "race": "concurrency",
    "race_condition": "concurrency",
    "database": "data",
    "data_integrity": "data",
    "persistence": "data",
    "backward_compatibility": "api",
    "compatibility": "api",
    "interface": "api",
    "efficiency": "performance",
    "resource_usage": "performance",
    "test": "test_gap",
    "tests": "test_gap",
    "testing": "test_gap",
    "test_coverage": "test_gap",
    "docs": "doc_defect",
    "documentation": "doc_defect",
    "code_quality": "style",
    "maintainability": "style",
    "naming": "style",
    "readability": "style",
    "hypothetical": "speculative",
    "risk": "speculative",
}
VERIFIER_DECISIONS = ("keep", "drop", "merge")
VERIFIER_MAX_BATCH = 32
MAX_FINDING_TITLE_CHARS = 300
MAX_FINDING_PATH_CHARS = 1_024
MAX_FINDING_EVIDENCE_CHARS = 8_000
MAX_FINDING_EXPLANATION_CHARS = 2_500


GENERATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            # Deliberately no maxItems: coverage must not be traded for an
            # arbitrary finding cap. Chunk sizing bounds each request instead.
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "path",
                    "side",
                    "line",
                    "end_line",
                    "severity",
                    "category",
                    "confidence",
                    "evidence",
                    "trigger",
                    "impact",
                    "suggested_fix",
                    "root_cause",
                    "failure_mode",
                    "fix_scope",
                ],
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_TITLE_CHARS,
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_PATH_CHARS,
                    },
                    "side": {"type": "string", "enum": ["RIGHT", "LEFT"]},
                    "line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "evidence": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EVIDENCE_CHARS,
                    },
                    "trigger": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EXPLANATION_CHARS,
                    },
                    "impact": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EXPLANATION_CHARS,
                    },
                    "suggested_fix": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EXPLANATION_CHARS,
                    },
                    "root_cause": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EXPLANATION_CHARS,
                    },
                    "failure_mode": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FINDING_EXPLANATION_CHARS,
                    },
                    "fix_scope": {
                        "type": "string",
                        "enum": ["local", "repeated_pattern", "systemic"],
                    },
                },
            },
        }
    },
}

# The transport boundary intentionally validates only the response envelope.
# Individual findings are normalized and validated independently below. This
# prevents one malformed proposal from discarding valid sibling proposals,
# while the persisted Finding contract remains fully strict and canonical.
GENERATION_TRANSPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {"type": "object"},
        }
    },
}


VERIFIER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": VERIFIER_MAX_BATCH,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "candidate_index",
                    "decision",
                    "confidence",
                    "reason",
                    "canonical_index",
                    "family_key",
                ],
                "properties": {
                    "candidate_index": {"type": "integer", "minimum": 0},
                    "decision": {
                        "type": "string",
                        "enum": list(VERIFIER_DECISIONS),
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reason": {"type": "string", "minLength": 1},
                    "canonical_index": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                    },
                    "family_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                        "pattern": "^[a-z0-9_]+$",
                    },
                },
            },
        }
    },
}

# Explicit aliases make call sites read naturally and keep the public contract
# stable if the descriptive names above change.
FINDINGS_SCHEMA = GENERATION_SCHEMA
VERIFICATION_SCHEMA = VERIFIER_SCHEMA


class PayloadValidationError(ValueError):
    """A model payload violates BugBunny's semantic output contract."""


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadValidationError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PayloadValidationError(f"{label} has " + "; ".join(details))


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PayloadValidationError(f"{label} must be a positive integer")
    return value


def _confidence(value: Any, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PayloadValidationError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise PayloadValidationError(f"{label} must be finite and between 0 and 1")
    return result


def _family_key(value: Any, *, label: str) -> str:
    result = _text(value, label=label)
    if len(result) > 160 or re.fullmatch(r"[a-z0-9_]+", result) is None:
        raise PayloadValidationError(f"{label} must be lowercase snake_case")
    return result


def _category(value: Any, *, label: str) -> str:
    raw = _text(value, label=label).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    result = CATEGORY_ALIASES.get(normalized, normalized)
    if result not in CATEGORIES:
        raise PayloadValidationError(f"{label} is not an allowed value")
    return result


def findings_from_payload(
    payload: Any,
    *,
    chunk_id: str,
) -> list[Finding]:
    """Validate generation output and convert it to the native finding model.

    Trigger and impact remain distinct in the wire contract so the model must
    state both. They are rendered into ``Finding.body`` because that is the
    compact native/reporting representation.
    """

    root = _object(payload, label="generation payload")
    _exact_keys(root, {"findings"}, label="generation payload")
    raw_findings = root["findings"]
    if not isinstance(raw_findings, list):
        raise PayloadValidationError("generation payload.findings must be an array")

    expected = {
        "title",
        "path",
        "side",
        "line",
        "end_line",
        "severity",
        "category",
        "confidence",
        "evidence",
        "trigger",
        "impact",
        "suggested_fix",
        "root_cause",
        "failure_mode",
        "fix_scope",
    }
    result: list[Finding] = []
    for index, raw in enumerate(raw_findings):
        item = _object(raw, label=f"findings[{index}]")
        _exact_keys(item, expected, label=f"findings[{index}]")
        line = _positive_int(item["line"], label=f"findings[{index}].line")
        end_line = _positive_int(item["end_line"], label=f"findings[{index}].end_line")
        if end_line < line:
            raise PayloadValidationError(f"findings[{index}].end_line must be at or after line")
        severity = _text(item["severity"], label=f"findings[{index}].severity")
        if severity not in SEVERITIES:
            raise PayloadValidationError(f"findings[{index}].severity is not an allowed value")
        category = _category(item["category"], label=f"findings[{index}].category")
        trigger = _text(item["trigger"], label=f"findings[{index}].trigger")
        impact = _text(item["impact"], label=f"findings[{index}].impact")
        fix_scope = _text(item["fix_scope"], label=f"findings[{index}].fix_scope")
        if fix_scope not in {"local", "repeated_pattern", "systemic"}:
            raise PayloadValidationError(
                f"findings[{index}].fix_scope is not an allowed value"
            )
        result.append(
            Finding(
                title=_text(item["title"], label=f"findings[{index}].title"),
                body=impact,
                path=_text(item["path"], label=f"findings[{index}].path"),
                side=_text(item["side"], label=f"findings[{index}].side"),  # type: ignore[arg-type]
                line=line,
                end_line=end_line,
                severity=severity,  # type: ignore[arg-type]
                category=category,  # type: ignore[arg-type]
                confidence=_confidence(item["confidence"], label=f"findings[{index}].confidence"),
                evidence=_text(item["evidence"], label=f"findings[{index}].evidence"),
                trigger=trigger,
                impact=impact,
                suggested_fix=_text(
                    item["suggested_fix"],
                    label=f"findings[{index}].suggested_fix",
                ),
                root_cause=_text(
                    item["root_cause"],
                    label=f"findings[{index}].root_cause",
                ),
                failure_mode=_text(
                    item["failure_mode"],
                    label=f"findings[{index}].failure_mode",
                ),
                fix_scope=fix_scope,
                chunk_id=chunk_id,
            )
        )
    return result


def findings_from_payload_tolerant(
    payload: Any,
    *,
    chunk_id: str,
) -> tuple[list[Finding], int]:
    """Keep valid findings while quarantining malformed sibling objects.

    The envelope remains fail-closed. Only item-level failures are isolated,
    and only the rejection count is returned so invalid model content cannot
    leak into artifacts or logs.
    """

    root = _object(payload, label="generation payload")
    _exact_keys(root, {"findings"}, label="generation payload")
    raw_findings = root["findings"]
    if not isinstance(raw_findings, list):
        raise PayloadValidationError("generation payload.findings must be an array")

    result: list[Finding] = []
    invalid_count = 0
    for raw in raw_findings:
        try:
            result.extend(findings_from_payload({"findings": [raw]}, chunk_id=chunk_id))
        except PayloadValidationError:
            invalid_count += 1
    return result, invalid_count


def validate_verifier_payload(
    payload: Any,
    *,
    candidate_count: int,
) -> list[dict[str, Any]]:
    """Validate a complete, one-decision-per-candidate verifier response."""

    if candidate_count < 0 or candidate_count > VERIFIER_MAX_BATCH:
        raise ValueError(f"candidate_count must be between 0 and {VERIFIER_MAX_BATCH}")
    root = _object(payload, label="verifier payload")
    _exact_keys(root, {"decisions"}, label="verifier payload")
    raw_decisions = root["decisions"]
    if not isinstance(raw_decisions, list):
        raise PayloadValidationError("verifier payload.decisions must be an array")
    if len(raw_decisions) != candidate_count:
        raise PayloadValidationError(
            "verifier payload must contain exactly one decision per candidate"
        )

    expected = {
        "candidate_index",
        "decision",
        "confidence",
        "reason",
        "canonical_index",
        "family_key",
    }
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for position, raw in enumerate(raw_decisions):
        item = _object(raw, label=f"decisions[{position}]")
        _exact_keys(item, expected, label=f"decisions[{position}]")
        index = item["candidate_index"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < candidate_count
        ):
            raise PayloadValidationError(f"decisions[{position}].candidate_index is out of range")
        if index in seen:
            raise PayloadValidationError(f"candidate {index} has more than one verifier decision")
        seen.add(index)
        decision = _text(item["decision"], label=f"decisions[{position}].decision")
        if decision not in VERIFIER_DECISIONS:
            raise PayloadValidationError(f"decisions[{position}].decision is not an allowed value")
        canonical = item["canonical_index"]
        if decision == "merge":
            if (
                not isinstance(canonical, int)
                or isinstance(canonical, bool)
                or not 0 <= canonical < index
            ):
                raise PayloadValidationError(
                    f"decisions[{position}].canonical_index must identify an earlier candidate"
                )
        elif canonical is not None:
            raise PayloadValidationError(
                f"decisions[{position}].canonical_index must be null for {decision}"
            )
        normalized.append(
            {
                "candidate_index": index,
                "decision": decision,
                "confidence": _confidence(
                    item["confidence"],
                    label=f"decisions[{position}].confidence",
                ),
                "reason": _text(item["reason"], label=f"decisions[{position}].reason"),
                "canonical_index": canonical,
                "family_key": _family_key(
                    item["family_key"], label=f"decisions[{position}].family_key"
                ),
            }
        )
    if seen != set(range(candidate_count)):
        raise PayloadValidationError("verifier omitted one or more candidates")
    ordered = sorted(normalized, key=lambda item: item["candidate_index"])
    for item in ordered:
        canonical = item["canonical_index"]
        if item["decision"] == "merge" and ordered[canonical]["decision"] != "keep":
            raise PayloadValidationError(
                f"candidate {item['candidate_index']} merges into a non-kept candidate"
            )
    return ordered


def finding_dicts(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    """Return a stable, JSON-ready projection used by the verifier prompt."""

    return [finding.to_dict() for finding in findings]
