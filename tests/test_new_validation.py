from __future__ import annotations

import pytest

from bugbunny.models import Finding, ReviewConfig
from bugbunny.validation import apply_verifier_decisions, validate_findings


def finding(**overrides) -> Finding:
    values = {
        "title": "Async callback is not awaited",
        "body": "forEach ignores the callback promise, so cleanup may finish early.",
        "path": "src/cleanup.ts",
        "side": "RIGHT",
        "line": 12,
        "end_line": 12,
        "severity": "high",
        "category": "concurrency",
        "confidence": 0.92,
        "evidence": "items.forEach(async (item) => {",
        "root_cause": "forEach discards the promise returned by its async callback.",
        "failure_mode": "The caller can finish before cleanup completes.",
        "fix_scope": "local",
        "trigger": "The array has at least one item and the callback rejects or outlives the caller.",
        "impact": "Cleanup may return early or surface an unhandled rejection.",
        "suggested_fix": "Use Promise.all(items.map(...)).",
        "chunk_id": "chunk-1",
    }
    values.update(overrides)
    return Finding(**values)


def config(*, profile: str = "balanced") -> ReviewConfig:
    return ReviewConfig(
        profile=profile,  # type: ignore[arg-type]
        verifier_model="same" if profile == "balanced" else "none",
    )


def source_at_line_12(line: str) -> str:
    return "\n" * 11 + line + "\n"


def test_validation_requires_changed_line_and_verbatim_evidence() -> None:
    accepted, rejected = validate_findings(
        [finding(), finding(line=13), finding(evidence="not in source")],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert len(accepted) == 1
    assert accepted[0].finding_id.startswith("bb-")
    assert {item.reason for item in rejected} == {
        "line is not an added-side changed line",
        "evidence is not present at head",
    }


def test_validation_preserves_and_accepts_changed_git_path_with_trailing_space() -> None:
    path = " src/clean\\up.ts "
    accepted, rejected = validate_findings(
        [finding(path=path)],
        changed_lines={path: {12}},
        read_source=lambda requested: (
            source_at_line_12("items.forEach(async (item) => {") if requested == path else ""
        ),
        chunk_locations={"chunk-1": (path, {12}, set())},
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not rejected
    assert [item.path for item in accepted] == [path]


def test_validation_rejects_location_outside_claimed_source_chunk() -> None:
    accepted, rejected = validate_findings(
        [finding(path="b.ts", line=1, end_line=1, evidence="buggy()", chunk_id="chunk-a")],
        changed_lines={"a.ts": {1}, "b.ts": {1}},
        read_source=lambda _path: "buggy()\n",
        chunk_locations={"chunk-a": ("a.ts", {1}, set())},
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not accepted
    assert rejected[0].reason == ("location is not attributable to the finding's source chunk")


def test_fast_profile_does_not_use_uncalibrated_generator_confidence_as_a_filter() -> None:
    accepted, rejected = validate_findings(
        [finding(confidence=0.2)],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        config=config(profile="fast"),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert len(accepted) == 1
    assert not rejected


@pytest.mark.parametrize("side", ["RIGHT", "LEFT"])
def test_validation_binds_duplicate_evidence_to_the_claimed_changed_line(side: str) -> None:
    source = "danger()\nsafe_context()\nother()\ndanger()\nwrong_context()\n"
    candidate = finding(
        path="src/repeated.ts",
        side=side,
        line=1,
        end_line=1,
        evidence="danger()\nwrong_context()",
    )
    accepted, rejected = validate_findings(
        [candidate],
        changed_lines={"src/repeated.ts": {1}} if side == "RIGHT" else {},
        read_source=lambda _path: source,
        deleted_lines={"src/repeated.ts": {1}} if side == "LEFT" else {},
        read_base_source=(lambda _path: source) if side == "LEFT" else None,
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not accepted
    assert rejected[0].reason == ("evidence occurrence is not anchored to the claimed changed line")


def test_validation_rejects_an_unpublishable_end_line() -> None:
    accepted, rejected = validate_findings(
        [finding(end_line=999_999)],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not accepted
    assert rejected[0].reason == "end_line is beyond the head file"


def test_one_bounded_source_read_failure_rejects_only_that_finding() -> None:
    accepted, rejected = validate_findings(
        [finding()],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: (_ for _ in ()).throw(RuntimeError("blob too large")),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not accepted
    assert rejected[0].reason == "source read failed: blob too large"


def test_dedup_does_not_merge_distinct_same_line_issues() -> None:
    first = finding(
        title="Promise is ignored",
        evidence="items.forEach(async (item) => { await remove(item); });",
    )
    second = finding(
        title="Mutation races between callbacks",
        body="Each callback mutates shared state concurrently.",
        evidence="items.forEach(async (item) => { await remove(item); });",
    )
    accepted, rejected = validate_findings(
        [first, second],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: source_at_line_12(
            "items.forEach(async (item) => { await remove(item); });"
        ),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert len(accepted) == 2
    assert not rejected


def test_dedup_preserves_identical_defects_at_distinct_sites() -> None:
    source = "buggy()\nkeep()\nbuggy()\n"
    accepted, rejected = validate_findings(
        [
            finding(path="src/repeat.ts", line=1, end_line=1, evidence="buggy()"),
            finding(path="src/repeat.ts", line=3, end_line=3, evidence="buggy()"),
        ],
        changed_lines={"src/repeat.ts": {1, 3}},
        read_source=lambda _path: source,
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert len(accepted) == 2
    assert accepted[0].fingerprint != accepted[1].fingerprint
    assert not rejected


def test_verifier_is_complete_and_fail_closed() -> None:
    findings = [finding(), finding(title="Second issue", evidence="await remove(item);")]
    with pytest.raises(ValueError, match="omitted"):
        apply_verifier_decisions(
            findings,
            {
                "decisions": [
                    {
                        "candidate_index": 0,
                        "decision": "keep",
                        "confidence": 0.9,
                        "reason": "proved",
                        "canonical_index": None,
                        "family_key": "async_callback_promise",
                    }
                ]
            },
            min_confidence=0.78,
        )


def test_verifier_filters_and_merges() -> None:
    findings = [finding(), finding(title="Duplicate wording")]
    kept, rejected = apply_verifier_decisions(
        findings,
        {
            "decisions": [
                {
                    "candidate_index": 0,
                    "decision": "keep",
                    "confidence": 0.91,
                    "reason": "proved",
                    "canonical_index": None,
                    "family_key": "async_callback_promise",
                },
                {
                    "candidate_index": 1,
                    "decision": "merge",
                    "confidence": 0.9,
                    "reason": "same root cause",
                    "canonical_index": 0,
                    "family_key": "async_callback_promise",
                },
            ]
        },
        min_confidence=0.78,
    )
    assert kept == [findings[0]]
    assert rejected[0].stage == "verifier_merge"


def test_merge_into_keep_below_operating_point_filters_without_failing_batch() -> None:
    findings = [finding(), finding(title="Duplicate wording")]
    kept, rejected = apply_verifier_decisions(
        findings,
        {
            "decisions": [
                {
                    "candidate_index": 0,
                    "decision": "keep",
                    "confidence": 0.90,
                    "reason": "plausible but below the frozen threshold",
                    "canonical_index": None,
                    "family_key": "async_callback_promise",
                },
                {
                    "candidate_index": 1,
                    "decision": "merge",
                    "confidence": 0.95,
                    "reason": "same cause and site",
                    "canonical_index": 0,
                    "family_key": "async_callback_promise",
                },
            ]
        },
        min_confidence=0.92,
    )
    assert kept == []
    assert {item.stage for item in rejected} == {"verifier_confidence", "verifier_merge"}


def test_line_numbering_follows_git_not_python_splitlines() -> None:
    # A form feed is an ordinary source byte for git; str.splitlines() would
    # split on it and shift every subsequent line number.
    source = "x\fy\nitems.forEach(async (item) => {\n"
    accepted, rejected = validate_findings(
        [finding(line=2, end_line=2)],
        changed_lines={"src/cleanup.ts": {2}},
        read_source=lambda _path: source,
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not rejected
    assert len(accepted) == 1


def test_semantic_fingerprint_distinguishes_diff_sides() -> None:
    right = finding(line=12, end_line=12)
    left = finding(side="LEFT", line=12, end_line=12)
    accepted, rejected = validate_findings(
        [right, left],
        changed_lines={"src/cleanup.ts": {12}},
        read_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        deleted_lines={"src/cleanup.ts": {12}},
        read_base_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert len(accepted) == 2
    assert not rejected
    assert accepted[0].fingerprint != accepted[1].fingerprint
    assert accepted[0].finding_id != accepted[1].finding_id


def test_left_side_rejections_name_the_base_revision() -> None:
    accepted, rejected = validate_findings(
        [finding(side="LEFT", line=999, end_line=999)],
        changed_lines={},
        read_source=lambda _path: "",
        deleted_lines={"src/cleanup.ts": {999}},
        read_base_source=lambda _path: source_at_line_12("items.forEach(async (item) => {"),
        config=config(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert not accepted
    assert rejected[0].reason == "line is beyond the base file"
