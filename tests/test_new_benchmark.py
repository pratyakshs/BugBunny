from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bugbunny import __version__
from bugbunny.benchmark import (
    artifact_model_directory,
    dedicated_fixture_tool,
    export_codereviewbench_results,
    load_codereviewbench_dataset,
    sanitize_model_name,
    tool_model_id,
    verify_codereviewbench_export_manifest,
)
from bugbunny.prompts import generation_prompt_sha256, verifier_prompt_sha256

GOLDEN_ONE = "https://github.com/acme/widget/pull/7"
GOLDEN_TWO = "https://github.com/acme/widget/pull/8"
FIXTURE_PRIMARY_ONE = (
    "https://github.com/code-review-benchmark/acme__widget__primarytool__PR7__20260821/pull/1"
)
FIXTURE_SECONDARY_ONE = (
    "https://github.com/code-review-benchmark/acme__widget__sampletool__PR7__20260821/pull/1"
)
FIXTURE_PRIMARY_TWO = (
    "https://github.com/code-review-benchmark/acme__widget__primarytool__PR8__20260821/pull/1"
)


def _entry(number: int, *, secondary: bool) -> dict:
    reviews = [
        {
            "tool": "primarytool",
            "repo_name": f"acme__widget__primarytool__PR{number}__20260821",
            "pr_url": FIXTURE_PRIMARY_ONE if number == 7 else FIXTURE_PRIMARY_TWO,
            "review_comments": [],
        }
    ]
    if secondary:
        reviews.append(
            {
                "tool": "sampletool",
                "repo_name": "acme__widget__sampletool__PR7__20260821",
                "pr_url": FIXTURE_SECONDARY_ONE,
                "review_comments": [],
            }
        )
    return {
        "pr_title": f"PR {number}",
        "source_repo": "widget",
        "golden_source_file": "widget.json",
        "golden_comments": [
            {"comment": f"Golden issue {number}", "severity": "High", "category": "bug"}
        ],
        "reviews": reviews,
    }


def _write_benchmark(path: Path) -> dict:
    value = {
        GOLDEN_ONE: _entry(7, secondary=True),
        GOLDEN_TWO: _entry(8, secondary=False),
    }
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return value


def _artifact(
    benchmark_path: Path,
    *,
    clean: bool = False,
    model: str = "openai/gpt-5.6-luna",
) -> dict:
    dataset = load_codereviewbench_dataset(benchmark_path, preferred_fixture_tool="sampletool")
    case = dataset.by_golden_url()[GOLDEN_ONE]
    findings = []
    if not clean:
        findings = [
            {
                "finding_id": "bb-" + "2" * 20,
                "fingerprint": "2" * 64,
                "title": "Second issue",
                "body": "The error is swallowed.",
                "path": "src/b.ts",
                "side": "RIGHT",
                "line": 21,
                "end_line": 21,
                "severity": "high",
                "category": "bug",
                "confidence": 0.96,
                "evidence": "catch (_) {}",
                "root_cause": "The added catch block discards every exception.",
                "failure_mode": "The caller observes success after the operation fails.",
                "fix_scope": "local",
                "trigger": "The operation throws.",
                "impact": "The error is swallowed.",
                "suggested_fix": "Propagate the error.",
                "chunk_id": "chunk-b",
            },
            {
                "finding_id": "bb-" + "1" * 20,
                "fingerprint": "1" * 64,
                "title": "First issue",
                "body": "The promise is not awaited.",
                "path": "src/a.ts",
                "side": "RIGHT",
                "line": 12,
                "end_line": 12,
                "severity": "critical",
                "category": "concurrency",
                "confidence": 0.98,
                "evidence": "items.forEach(async ...)",
                "root_cause": "forEach discards the async callback promise.",
                "failure_mode": "The operation completes before its work finishes.",
                "fix_scope": "local",
                "trigger": "The callback suspends.",
                "impact": "The promise is not awaited.",
                "suggested_fix": "Await Promise.all.",
                "chunk_id": "chunk-a",
            },
        ]
    return {
        "schema_version": "bugbunny-review-v2",
        "tool": "bugbunny",
        "tool_version": __version__,
        "status": "completed",
        "completed_at": "2026-08-21T01:02:03Z",
        "config": {"model": model},
        "pr": {
            "url": FIXTURE_SECONDARY_ONE,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
        },
        "runtime": {"transport": "test", "requested_model": model},
        "context": {
            "generation_prompt_sha256": generation_prompt_sha256(),
            "verifier_prompt_sha256": verifier_prompt_sha256(),
        },
        "coverage": {"complete": True},
        "diff": {
            "chunk_plan_complete": True,
            "commentable_ranges": {
                "RIGHT": {"src/a.ts": [[12, 12]], "src/b.ts": [[21, 21]]},
                "LEFT": {},
            },
        },
        "benchmark": {
            "suite": "CodeReviewBench",
            "case_id": case.case_id,
            "golden_url": GOLDEN_ONE,
            "review_url": FIXTURE_SECONDARY_ONE,
            "fixture_tool": "sampletool",
            "golden_sha256": case.golden_sha256,
            "benchmark_sha256": dataset.manifest.benchmark_sha256,
            "dataset_golden_sha256": dataset.manifest.golden_sha256,
        },
        "findings": findings,
        "validated_findings": findings,
    }


def test_loader_selects_requested_fixture_then_default_without_retaining_goldens(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)

    dataset = load_codereviewbench_dataset(
        benchmark_path,
        preferred_fixture_tool="sampletool",
        expected_case_count=2,
    )

    assert dataset.manifest.case_count == 2
    assert dataset.manifest.golden_issue_count == 2
    assert len(dataset.manifest.benchmark_sha256) == 64
    assert len(dataset.manifest.golden_sha256) == 64
    first, second = dataset.cases
    assert first.golden_url == GOLDEN_ONE
    assert first.review_url == FIXTURE_SECONDARY_ONE
    assert first.fixture_tool == "sampletool"
    assert second.review_url == FIXTURE_PRIMARY_TWO
    assert second.fixture_tool == "primarytool"
    assert "golden" not in json.dumps(first.to_engine_input()).lower()
    assert first.to_engine_input()["pr_url"] == FIXTURE_SECONDARY_ONE
    assert not hasattr(first, "golden_comments")

    with pytest.raises(ValueError, match="expected 50"):
        load_codereviewbench_dataset(benchmark_path, expected_case_count=50)
    with pytest.raises(ValueError, match="no fixture for tool"):
        load_codereviewbench_dataset(
            benchmark_path,
            preferred_fixture_tool="sampletool",
            require_preferred_tool=True,
        )


def test_export_is_schema_compatible_lossless_and_deterministic(tmp_path: Path) -> None:
    benchmark_path = tmp_path / "source" / "benchmark_data.json"
    benchmark_path.parent.mkdir()
    original = _write_benchmark(benchmark_path)
    results = tmp_path / "results"
    judge_dir = results / sanitize_model_name("openai/gpt-5.2")
    judge_dir.mkdir(parents=True)
    (judge_dir / "candidates.json").write_text(
        json.dumps({GOLDEN_ONE: {"other": [{"text": "keep"}]}}), encoding="utf-8"
    )
    (judge_dir / "dedup_groups.json").write_text(
        json.dumps({GOLDEN_ONE: {"other": [[0]]}}), encoding="utf-8"
    )

    exported = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=results,
        judge_model="openai/gpt-5.2",
        expected_case_count=2,
    )
    first_bytes = {
        path.name: path.read_bytes()
        for path in (
            exported.benchmark_data_path,
            exported.candidates_path,
            exported.dedup_groups_path,
            exported.manifest_path,
        )
    }
    exported_again = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=results,
        judge_model="openai/gpt-5.2",
        expected_case_count=2,
    )
    second_bytes = {
        path.name: path.read_bytes()
        for path in (
            exported_again.benchmark_data_path,
            exported_again.candidates_path,
            exported_again.dedup_groups_path,
            exported_again.manifest_path,
        )
    }
    assert first_bytes == second_bytes

    tool_id = exported.tool_id
    assert exported.tool_id == tool_id
    assert sanitize_model_name("openai/gpt-5.2") == "openai_gpt-5.2"
    benchmark_data = json.loads(exported.benchmark_data_path.read_text())
    candidates = json.loads(exported.candidates_path.read_text())
    dedup = json.loads(exported.dedup_groups_path.read_text())

    # Ground truth and unrelated reviews are copied exactly.
    assert benchmark_data[GOLDEN_ONE]["golden_comments"] == original[GOLDEN_ONE]["golden_comments"]
    reviews = benchmark_data[GOLDEN_ONE]["reviews"]
    assert sum(review["tool"] == tool_id for review in reviews) == 1
    review = next(review for review in reviews if review["tool"] == tool_id)
    assert review["pr_url"] == FIXTURE_SECONDARY_ONE
    assert len(review["review_comments"]) == 2
    assert review["review_comments"][0]["path"] == "src/a.ts"
    assert review["review_comments"][0]["line"] == 12

    assert candidates[GOLDEN_ONE]["other"] == [{"text": "keep"}]
    direct = candidates[GOLDEN_ONE][tool_id]
    assert [set(item) for item in direct] == [
        {"text", "path", "line", "source"},
        {"text", "path", "line", "source"},
    ]
    assert [(item["path"], item["line"]) for item in direct] == [
        ("src/a.ts", 12),
        ("src/b.ts", 21),
    ]
    assert direct[0]["text"].startswith("Location: src/a.ts:12 (RIGHT)")
    assert direct[0]["text"] != direct[1]["text"]
    assert dedup[GOLDEN_ONE]["other"] == [[0]]
    assert dedup[GOLDEN_ONE][tool_id] == [[0], [1]]
    assert exported.input_golden_sha256 == exported.output_golden_sha256
    assert exported.output_files_sha256 == {
        "benchmark_data.json": hashlib.sha256(
            exported.benchmark_data_path.read_bytes()
        ).hexdigest(),
        "openai_gpt-5.2/candidates.json": hashlib.sha256(
            exported.candidates_path.read_bytes()
        ).hexdigest(),
        "openai_gpt-5.2/dedup_groups.json": hashlib.sha256(
            exported.dedup_groups_path.read_bytes()
        ).hexdigest(),
    }
    verified = verify_codereviewbench_export_manifest(exported.manifest_path)
    assert verified["ok"] is True
    assert verified["manifest_sha256"] == exported.manifest_sha256
    assert verified["candidate_count"] == 2


def test_export_accepts_original_fixture_when_an_existing_tool_reuses_its_url(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    benchmark_data = _write_benchmark(benchmark_path)
    benchmark_data[GOLDEN_ONE]["reviews"].append(
        {
            "tool": "bugbunny-existing-export",
            "repo_name": "existing",
            "pr_url": FIXTURE_SECONDARY_ONE,
            "review_comments": [],
        }
    )
    benchmark_path.write_text(json.dumps(benchmark_data, indent=2), encoding="utf-8")

    exported = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=tmp_path / "results",
        judge_model="judge/model",
    )

    assert exported.manifest_path.is_file()


def test_model_sweep_keeps_one_manifest_per_review_model(tmp_path: Path) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    results = tmp_path / "results"
    first = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path, model="openai/model-a")},
        output_dir=results,
        judge_model="judge/model",
    )
    second = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path, model="openai/model-b")},
        output_dir=results,
        judge_model="judge/model",
    )

    assert first.manifest_path != second.manifest_path
    assert first.manifest_path.is_file()
    assert second.manifest_path.is_file()
    candidates = json.loads(second.candidates_path.read_text(encoding="utf-8"))
    assert set(candidates[GOLDEN_ONE]) >= {
        first.tool_id,
        second.tool_id,
    }
    # The judge files are shared across review models, so adding the second
    # model refreshes both manifests to one final committed bundle identity.
    final_hashes = json.loads(second.manifest_path.read_text())["output_files_sha256"]
    assert json.loads(first.manifest_path.read_text())["output_files_sha256"] == final_hashes
    assert verify_codereviewbench_export_manifest(first.manifest_path)["ok"] is True
    assert verify_codereviewbench_export_manifest(second.manifest_path)["ok"] is True


def test_model_identifiers_remain_distinct_after_lossy_sanitization() -> None:
    first = "openai/a/b"
    second = "openai/a_b"
    assert sanitize_model_name(first) == sanitize_model_name(second)
    assert artifact_model_directory(first) != artifact_model_directory(second)
    assert tool_model_id("bugbunny", first) != tool_model_id("bugbunny", second)
    assert len(dedicated_fixture_tool("bugbunny", first)) <= 30
    assert dedicated_fixture_tool("bugbunny", first) != dedicated_fixture_tool("bugbunny", second)


def test_clean_artifact_still_exports_an_empty_review_for_recall_accounting(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    exported = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path, clean=True)},
        output_dir=tmp_path / "results",
        judge_model="judge/model",
    )
    tool_id = exported.tool_id
    benchmark_data = json.loads(exported.benchmark_data_path.read_text())
    candidates = json.loads(exported.candidates_path.read_text())
    dedup = json.loads(exported.dedup_groups_path.read_text())
    review = next(
        value for value in benchmark_data[GOLDEN_ONE]["reviews"] if value["tool"] == tool_id
    )
    assert review["review_comments"] == []
    assert candidates[GOLDEN_ONE][tool_id] == []
    assert dedup[GOLDEN_ONE][tool_id] == []


def test_export_tracks_have_distinct_identities_and_family_track_preserves_locations(
    tmp_path: Path,
) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    artifact = _artifact(benchmark_path)
    for finding in artifact["findings"]:
        finding["category"] = "bug"
        finding["root_cause"] = "A repeated nullable lookup is dereferenced."
        finding["failure_mode"] = "A missing record raises during the request."
        finding["verifier_confidence"] = 0.93
        finding["verifier_family_key"] = "nullable_lookup"
    artifact["validated_findings"] = [dict(item) for item in artifact["findings"]]
    results = tmp_path / "results"

    generator = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: artifact},
        output_dir=results,
        judge_model="judge/model",
        finding_stage="generator",
    )
    balanced = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: artifact},
        output_dir=results,
        judge_model="judge/model",
        finding_stage="balanced",
    )
    family = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: artifact},
        output_dir=results,
        judge_model="judge/model",
        finding_stage="family",
    )

    assert len({generator.tool_id, balanced.tool_id, family.tool_id}) == 3
    assert generator.candidate_count == balanced.candidate_count == 2
    assert family.candidate_count == 1
    candidates = json.loads(family.candidates_path.read_text())
    family_text = candidates[GOLDEN_ONE][family.tool_id][0]["text"]
    assert "Related locations:" in family_text
    audit = json.loads(family.candidate_audit_path.read_text())
    assert len(audit["cases"][GOLDEN_ONE][0]["family_member_ids"]) == 2


def test_export_rejects_stale_or_non_fixture_artifact_provenance(tmp_path: Path) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    stale = _artifact(benchmark_path)
    stale["benchmark"]["golden_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="golden_sha256"):
        export_codereviewbench_results(
            benchmark_path,
            {GOLDEN_ONE: stale},
            output_dir=tmp_path / "results-a",
            judge_model="judge/model",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("path", "../../outside", "unsafe path"),
        ("side", "MIDDLE", "invalid diff side"),
        ("line", 999_999, "changed-line ledger"),
        ("finding_id", "", "finding ID"),
    ],
)
def test_export_revalidates_final_finding_internals(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    artifact = _artifact(benchmark_path)
    artifact["findings"][0][field] = value
    if field == "line":
        artifact["findings"][0]["end_line"] = value

    with pytest.raises(ValueError, match=message):
        export_codereviewbench_results(
            benchmark_path,
            {GOLDEN_ONE: artifact},
            output_dir=tmp_path / "results",
            judge_model="judge/model",
        )


def test_subset_reexport_purges_stale_cases_for_the_same_tool_id(tmp_path: Path) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    results = tmp_path / "results"
    first = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=results,
        judge_model="judge/model",
    )
    benchmark_data = json.loads(first.benchmark_data_path.read_text(encoding="utf-8"))
    benchmark_data[GOLDEN_TWO]["reviews"].append(
        {
            "tool": first.tool_id,
            "repo_name": "stale",
            "pr_url": FIXTURE_PRIMARY_TWO,
            "review_comments": [{"body": "stale"}],
        }
    )
    first.benchmark_data_path.write_text(json.dumps(benchmark_data), encoding="utf-8")
    candidates = json.loads(first.candidates_path.read_text(encoding="utf-8"))
    candidates.setdefault(GOLDEN_TWO, {})[first.tool_id] = [{"text": "stale"}]
    first.candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    groups = json.loads(first.dedup_groups_path.read_text(encoding="utf-8"))
    groups.setdefault(GOLDEN_TWO, {})[first.tool_id] = [[0]]
    first.dedup_groups_path.write_text(json.dumps(groups), encoding="utf-8")

    second = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=results,
        judge_model="judge/model",
    )
    output_data = json.loads(second.benchmark_data_path.read_text(encoding="utf-8"))
    output_candidates = json.loads(second.candidates_path.read_text(encoding="utf-8"))
    output_groups = json.loads(second.dedup_groups_path.read_text(encoding="utf-8"))

    assert not any(
        review.get("tool") == first.tool_id for review in output_data[GOLDEN_TWO]["reviews"]
    )
    assert first.tool_id not in output_candidates.get(GOLDEN_TWO, {})
    assert first.tool_id not in output_groups.get(GOLDEN_TWO, {})

    wrong_pr = _artifact(benchmark_path)
    wrong_pr["pr"]["url"] = GOLDEN_ONE
    with pytest.raises(ValueError, match="fixture URL"):
        export_codereviewbench_results(
            benchmark_path,
            {GOLDEN_ONE: wrong_pr},
            output_dir=tmp_path / "results-b",
            judge_model="judge/model",
        )


def test_export_verifier_rejects_post_commit_candidate_tampering(tmp_path: Path) -> None:
    benchmark_path = tmp_path / "benchmark_data.json"
    _write_benchmark(benchmark_path)
    exported = export_codereviewbench_results(
        benchmark_path,
        {GOLDEN_ONE: _artifact(benchmark_path)},
        output_dir=tmp_path / "results",
        judge_model="judge/model",
    )
    candidates = json.loads(exported.candidates_path.read_text(encoding="utf-8"))
    candidates[GOLDEN_ONE][exported.tool_id][0]["text"] = "tampered after export"
    exported.candidates_path.write_text(json.dumps(candidates), encoding="utf-8")

    with pytest.raises(ValueError, match="do not match the export manifest"):
        verify_codereviewbench_export_manifest(exported.manifest_path)
