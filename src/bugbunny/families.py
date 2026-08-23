"""Conservative full-review deduplication and issue-family presentation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from bugbunny.models import Finding, RejectedFinding

_WORDS = re.compile(r"[a-z0-9_]+")
_STOP_WORDS = {
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
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "when",
    "with",
}


def _tokens(value: str) -> frozenset[str]:
    return frozenset(word for word in _WORDS.findall(value.lower()) if word not in _STOP_WORDS)


def _causal_tokens(finding: Finding | Mapping[str, Any]) -> frozenset[str]:
    if isinstance(finding, Finding):
        values = (
            finding.root_cause,
            finding.failure_mode,
            finding.trigger,
            finding.suggested_fix,
        )
    else:
        values = tuple(
            str(finding.get(field) or "")
            for field in ("root_cause", "failure_mode", "trigger", "suggested_fix")
        )
    return _tokens(" ".join(values))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _same_site(left: Finding, right: Finding) -> bool:
    return (
        left.path,
        left.side,
        left.line,
        left.end_line,
    ) == (
        right.path,
        right.side,
        right.line,
        right.end_line,
    )


def consolidate_semantic_duplicates(
    findings: Sequence[Finding],
) -> tuple[list[Finding], list[RejectedFinding]]:
    """Remove only near-certain paraphrases at the exact same review location.

    Independent occurrences deliberately survive, including repeated defects on
    adjacent lines. Issue-family grouping is a presentation layer and handles
    those separately without erasing atomic evidence from the artifact.
    """

    kept: list[Finding] = []
    rejected: list[RejectedFinding] = []
    for finding in findings:
        duplicate_of: Finding | None = None
        finding_tokens = _causal_tokens(finding)
        for canonical in kept:
            same_family = bool(
                finding.verifier_family_key
                and finding.verifier_family_key == canonical.verifier_family_key
            )
            if _same_site(finding, canonical) and (
                same_family or _jaccard(finding_tokens, _causal_tokens(canonical)) >= 0.82
            ):
                duplicate_of = canonical
                break
        if duplicate_of is None:
            kept.append(finding)
            continue
        rejected.append(
            RejectedFinding(
                finding=replace(finding),
                stage="semantic_duplicate",
                reason=f"same causal claim and exact location as {duplicate_of.finding_id}",
            )
        )
    return kept, rejected


@dataclass(frozen=True)
class FindingFamily:
    family_id: str
    members: tuple[dict[str, Any], ...]

    @property
    def primary(self) -> dict[str, Any]:
        return self.members[0]


def group_finding_families(findings: Iterable[Mapping[str, Any]]) -> tuple[FindingFamily, ...]:
    """Group verified repeated patterns while retaining every member location.

    A verifier label alone is insufficient: members must also share a category
    and have materially overlapping causal descriptions. This keeps generic
    model labels from collapsing unrelated findings.
    """

    ordered = sorted(
        (dict(item) for item in findings),
        key=lambda item: (
            str(item.get("path") or ""),
            int(item.get("line") or 0),
            str(item.get("finding_id") or ""),
        ),
    )
    groups: list[list[dict[str, Any]]] = []
    for finding in ordered:
        key = str(finding.get("verifier_family_key") or "").strip()
        tokens = _causal_tokens(finding)
        target: list[dict[str, Any]] | None = None
        if key:
            for group in groups:
                primary = group[0]
                if (
                    key == str(primary.get("verifier_family_key") or "").strip()
                    and finding.get("category") == primary.get("category")
                    and _jaccard(
                        _tokens(str(finding.get("root_cause") or "")),
                        _tokens(str(primary.get("root_cause") or "")),
                    )
                    >= 0.40
                    and _jaccard(tokens, _causal_tokens(primary)) >= 0.45
                ):
                    target = group
                    break
        if target is None:
            groups.append([finding])
        else:
            target.append(finding)

    result: list[FindingFamily] = []
    for group in groups:
        group.sort(
            key=lambda item: (
                -float(item.get("verifier_confidence") or 0.0),
                str(item.get("path") or ""),
                int(item.get("line") or 0),
                str(item.get("finding_id") or ""),
            )
        )
        primary = group[0]
        family_id = str(primary.get("verifier_family_key") or primary.get("finding_id") or "")
        result.append(FindingFamily(family_id=family_id, members=tuple(group)))
    return tuple(result)


__all__ = [
    "FindingFamily",
    "consolidate_semantic_duplicates",
    "group_finding_families",
]
