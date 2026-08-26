from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from bugbunny.models import Finding, RejectedFinding, ReviewConfig
from bugbunny.util import git_lines

VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_CATEGORIES = {
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
}


def changed_line_ranges(lines_by_path: dict[str, set[int]]) -> dict[str, list[list[int]]]:
    """Compact a changed-line ledger into deterministic inclusive ranges."""

    result: dict[str, list[list[int]]] = {}
    for path, values in sorted(lines_by_path.items()):
        numbers = sorted(number for number in values if number > 0)
        ranges: list[list[int]] = []
        for number in numbers:
            if ranges and number == ranges[-1][1] + 1:
                ranges[-1][1] = number
            else:
                ranges.append([number, number])
        result[path] = ranges
    return result


def artifact_location_is_commentable(
    diff: Any,
    *,
    path: str,
    side: str,
    line: int,
    end_line: int,
) -> bool:
    """Validate an artifact anchor against its deterministic changed-line ledger."""

    if not isinstance(diff, dict):
        return False
    ledger = diff.get("commentable_ranges")
    if not isinstance(ledger, dict):
        return False
    side_ranges = ledger.get(side)
    if not isinstance(side_ranges, dict):
        return False
    ranges = side_ranges.get(path)
    if not isinstance(ranges, list):
        return False

    def included(number: int) -> bool:
        return any(
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
            and 0 < value[0] <= number <= value[1]
            for value in ranges
        )

    return included(line) and included(end_line)


def _normalized_space(value: str) -> str:
    return " ".join(value.split())


def _normalized_source_with_line_map(source: str) -> tuple[str, tuple[int, ...]]:
    """Normalize source whitespace while retaining each character's source line."""

    output: list[str] = []
    line_map: list[int] = []
    previous_line: int | None = None
    for line_number, line in enumerate(git_lines(source), start=1):
        for token in line.split():
            if output:
                output.append(" ")
                # A separator between tokens on one physical line belongs to
                # that line. Cross-line separators deliberately belong to no
                # line so an anchor cannot be credited to an adjacent copy.
                line_map.append(line_number if previous_line == line_number else 0)
            output.extend(token)
            line_map.extend([line_number] * len(token))
            previous_line = line_number
    return "".join(output), tuple(line_map)


def _evidence_occurs_at_line(source: str, evidence: str, line: int, anchor: str) -> bool:
    """Return whether one evidence occurrence contains the anchor at ``line``."""

    normalized_source, source_lines = _normalized_source_with_line_map(source)
    normalized_evidence = _normalized_space(evidence)
    normalized_anchor = _normalized_space(anchor)
    if not normalized_evidence or not normalized_anchor:
        return False
    anchor_positions = [
        index for index, mapped_line in enumerate(source_lines) if mapped_line == line
    ]
    if not anchor_positions:
        return False
    first_anchor = anchor_positions[0]
    last_anchor = anchor_positions[-1]
    if normalized_source[first_anchor : last_anchor + 1] != normalized_anchor:
        return False

    # An anchored occurrence must start before the physical anchor and finish
    # after it. Checking only that bounded interval avoids scanning every copy
    # of a short token in a large or adversarially repetitive source file.
    first_start = max(0, last_anchor - len(normalized_evidence) + 1)
    last_start = min(first_anchor, len(normalized_source) - len(normalized_evidence))
    return any(
        normalized_source.startswith(normalized_evidence, start)
        for start in range(first_start, last_start + 1)
    )


def _canonical_path(value: str) -> str | None:
    value = value.strip().replace("\\", "/")
    path = PurePosixPath(value)
    if not value or not path.parts or path.is_absolute() or ".." in path.parts or "." in path.parts:
        return None
    return str(path)


def _semantic_fingerprint(finding: Finding) -> str:
    words = re.findall(r"[a-z0-9_]+", f"{finding.title} {finding.body}".lower())
    stop = {
        "a",
        "an",
        "and",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
    semantic = " ".join(word for word in words if word not in stop)
    evidence = _normalized_space(finding.evidence).lower()
    # Location is part of identity: two independently fixable occurrences of
    # the same buggy expression must remain two benchmark candidates. The
    # verifier can still merge paraphrases that describe one site. The side is
    # part of the location: a deleted-line and an added-line finding at equal
    # coordinates are distinct sites.
    raw = (
        f"{finding.path}\0{finding.side}\0{finding.line}\0{finding.end_line}"
        f"\0{semantic}\0{evidence}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _finding_id(base_sha: str, head_sha: str, fingerprint: str) -> str:
    raw = f"bugbunny-v2\0{base_sha}\0{head_sha}\0{fingerprint}"
    return "bb-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def validate_findings(
    findings: Iterable[Finding],
    *,
    changed_lines: dict[str, set[int]],
    read_source: Callable[[str], str],
    deleted_lines: dict[str, set[int]] | None = None,
    read_base_source: Callable[[str], str] | None = None,
    config: ReviewConfig,
    base_sha: str,
    head_sha: str,
) -> tuple[list[Finding], list[RejectedFinding]]:
    """Apply evidence and location gates without using model self-confidence.

    The verifier, when enabled, owns confidence calibration. Generator
    self-confidence remains telemetry rather than a cross-model filter because
    it is not calibrated consistently across providers.
    """

    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []
    seen: set[str] = set()
    deleted_lines = deleted_lines or {}

    for original in findings:
        finding = replace(original)
        path = _canonical_path(finding.path)
        if path is None:
            rejected.append(RejectedFinding(finding, "validation", "unsafe or empty path"))
            continue
        finding.path = path

        if finding.side == "RIGHT":
            allowed_lines = changed_lines
            source_reader = read_source
            side_label = "added-side"
            side_article = "an"
            revision_label = "head"
        elif finding.side == "LEFT":
            allowed_lines = deleted_lines
            source_reader = read_base_source
            side_label = "deleted-side"
            side_article = "a"
            revision_label = "base"
        else:
            rejected.append(RejectedFinding(finding, "validation", "unknown diff side"))
            continue
        if source_reader is None:
            rejected.append(
                RejectedFinding(finding, "validation", f"{finding.side} source is unavailable")
            )
            continue
        if path not in allowed_lines:
            rejected.append(
                RejectedFinding(finding, "validation", "path is not a changed text file")
            )
            continue
        if finding.line <= 0 or finding.line not in allowed_lines[path]:
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    f"line is not {side_article} {side_label} changed line",
                )
            )
            continue
        if finding.end_line < finding.line:
            rejected.append(RejectedFinding(finding, "validation", "end_line precedes line"))
            continue
        if finding.severity not in VALID_SEVERITIES:
            rejected.append(RejectedFinding(finding, "validation", "unknown severity"))
            continue
        if finding.category not in VALID_CATEGORIES:
            rejected.append(RejectedFinding(finding, "validation", "unknown category"))
            continue
        if finding.category not in config.include_categories:
            rejected.append(
                RejectedFinding(finding, "policy", "category excluded by review profile")
            )
            continue
        if not math.isfinite(finding.confidence) or not 0 <= finding.confidence <= 1:
            rejected.append(RejectedFinding(finding, "validation", "confidence is outside [0, 1]"))
            continue
        if not finding.title or not finding.body or not finding.trigger or not finding.impact:
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    "title, body, trigger, and impact are required",
                )
            )
            continue
        if not finding.root_cause or not finding.failure_mode:
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    "root_cause and failure_mode are required",
                )
            )
            continue
        if finding.fix_scope not in {"local", "repeated_pattern", "systemic"}:
            rejected.append(RejectedFinding(finding, "validation", "unknown fix_scope"))
            continue
        if not finding.evidence:
            rejected.append(
                RejectedFinding(finding, "validation", "nonempty verbatim evidence is required")
            )
            continue

        try:
            source = source_reader(path)
        except (OSError, ValueError, RuntimeError) as exc:
            rejected.append(RejectedFinding(finding, "validation", f"source read failed: {exc}"))
            continue
        normalized_evidence = _normalized_space(finding.evidence)
        if normalized_evidence not in _normalized_space(source):
            rejected.append(
                RejectedFinding(
                    finding, "validation", f"evidence is not present at {revision_label}"
                )
            )
            continue
        # Git counts only "\n" as a line terminator; splitting on anything more
        # would desynchronize these indices from the diff's changed-line ledger.
        source_lines = git_lines(source)
        if finding.line > len(source_lines):
            rejected.append(
                RejectedFinding(finding, "validation", f"line is beyond the {revision_label} file")
            )
            continue
        if finding.end_line > len(source_lines):
            rejected.append(
                RejectedFinding(finding, "validation", f"end_line is beyond the {revision_label} file")
            )
            continue
        if finding.end_line not in allowed_lines[path]:
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    f"end_line is not {side_article} {side_label} changed line",
                )
            )
            continue
        anchor = _normalized_space(source_lines[finding.line - 1])
        if not anchor or anchor not in normalized_evidence:
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    f"evidence does not contain the complete {side_label} anchor line",
                )
            )
            continue
        if not _evidence_occurs_at_line(
            source,
            finding.evidence,
            finding.line,
            source_lines[finding.line - 1],
        ):
            rejected.append(
                RejectedFinding(
                    finding,
                    "validation",
                    "evidence occurrence is not anchored to the claimed changed line",
                )
            )
            continue

        finding.fingerprint = _semantic_fingerprint(finding)
        finding.finding_id = _finding_id(base_sha, head_sha, finding.fingerprint)
        if finding.fingerprint in seen:
            rejected.append(RejectedFinding(finding, "dedup", "exact semantic/evidence duplicate"))
            continue
        seen.add(finding.fingerprint)
        accepted.append(finding)

    return accepted, rejected


def apply_verifier_decisions(
    findings: list[Finding],
    payload: dict[str, Any],
    *,
    min_confidence: float,
) -> tuple[list[Finding], list[RejectedFinding]]:
    """Apply one verifier batch strictly and reproducibly.

    Every candidate must receive exactly one decision. Missing, duplicate, or
    malformed decisions raise ValueError so callers can fail closed and retain
    the untouched raw/validated streams in the artifact.
    """

    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("verifier response has no decisions array")
    by_index: dict[int, dict[str, Any]] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("verifier decision is not an object")
        index = decision.get("candidate_index")
        if not isinstance(index, int) or index < 0 or index >= len(findings):
            raise ValueError("verifier decision index is out of range")
        if index in by_index:
            raise ValueError("verifier returned a duplicate decision index")
        by_index[index] = decision
    if set(by_index) != set(range(len(findings))):
        raise ValueError("verifier omitted one or more finding decisions")

    kept: list[Finding] = []
    rejected: list[RejectedFinding] = []
    canonical_keep_verdicts: set[int] = set()
    for index, finding in enumerate(findings):
        decision = by_index[index]
        verdict = str(decision.get("decision", "drop")).lower()
        confidence = float(decision.get("confidence", 0.0) or 0.0)
        reason = str(decision.get("reason", "")).strip() or "no verifier reason"
        finding.verifier_confidence = confidence
        finding.verifier_reason = reason
        finding.verifier_family_key = str(decision.get("family_key", "")).strip() or None

        if verdict == "merge":
            canonical = decision.get("canonical_index")
            if not isinstance(canonical, int) or canonical < 0 or canonical >= len(findings):
                raise ValueError("merge decision has an invalid canonical_index")
            if canonical == index:
                raise ValueError("merge decision cannot point to itself")
            rejected.append(
                RejectedFinding(finding, "verifier_merge", f"merged into finding {canonical}")
            )
            continue
        if verdict != "keep":
            rejected.append(RejectedFinding(finding, "verifier", reason))
            continue
        canonical_keep_verdicts.add(index)
        if confidence < min_confidence:
            rejected.append(
                RejectedFinding(
                    finding,
                    "verifier_confidence",
                    f"{confidence:.3f} is below {min_confidence:.3f}: {reason}",
                )
            )
            continue
        kept.append(finding)

    # A merge target must have a keep verdict. It need not survive the calibrated
    # confidence threshold: in that case both the canonical and its duplicate
    # are correctly filtered, rather than turning a valid low-confidence batch
    # into a protocol failure.
    for index, decision in by_index.items():
        if str(decision.get("decision", "")).lower() == "merge":
            canonical = int(decision["canonical_index"])
            if canonical not in canonical_keep_verdicts:
                raise ValueError(
                    f"finding {index} merges into non-kept canonical finding {canonical}"
                )
    return kept, rejected


def finding_text(finding: Finding) -> str:
    """Render one benchmark candidate without bundling multiple issues."""

    suffix = f"\n\nSuggested direction: {finding.suggested_fix}" if finding.suggested_fix else ""
    return f"{finding.title}\n\nTrigger: {finding.trigger}\n\nImpact: {finding.impact}{suffix}"
