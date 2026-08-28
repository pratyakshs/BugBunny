from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
from typing import Any

import httpx
import pytest

from bugbunny.judge import (
    JUDGE_PROMPT,
    JudgeConfig,
    JudgeError,
    MartianJudge,
    evaluate_review,
    get_candidates,
    run_codereviewbench_judge,
)


def _run_concurrent_judge_process(
    results_dir: str,
    evaluations_file: str,
    tool: str,
    start: Any,
) -> None:
    class SlowJudge:
        async def match_comment(self, golden: str, candidate: str) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return {
                "match": golden == candidate,
                "confidence": 0.9,
                "reasoning": "same" if golden == candidate else "different",
            }

    if not start.wait(10):
        raise RuntimeError("concurrent judge start was not released")
    asyncio.run(
        run_codereviewbench_judge(
            results_dir=Path(results_dir),
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            tools=[tool],
            evaluations_file=Path(evaluations_file),
            judge=SlowJudge(),
        )
    )


def _run_paused_export_process(results_dir: str, ready: Any, finish: Any) -> None:
    from bugbunny.util import atomic_write_json, file_lock

    root = Path(results_dir)
    model_dir = root / "anthropic_judge"
    with file_lock(root / ".bugbunny-export.lock"):
        benchmark_path = root / "benchmark_data.json"
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
        golden_url = next(iter(benchmark))
        benchmark[golden_url]["golden_comments"][0]["comment"] = "new consistent issue"
        atomic_write_json(benchmark_path, benchmark)
        ready.set()
        if not finish.wait(10):
            raise RuntimeError("paused export was not released")
        candidates_path = model_dir / "candidates.json"
        candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates[golden_url]["tool-a"][0]["text"] = "new consistent issue"
        atomic_write_json(candidates_path, candidates)


def test_candidates_prefer_export_and_fall_back_to_review_comments() -> None:
    review = {"tool": "tool-a", "review_comments": [{"body": "raw"}]}
    cached = {"url": {"tool-a": [{"text": "cached"}, {"text": ""}]}}

    assert get_candidates(review, cached, "url") == ["cached"]
    assert get_candidates(review, {}, "url") == ["raw"]


@pytest.mark.asyncio
async def test_evaluate_review_preserves_pair_order_and_dedup_metrics() -> None:
    responses = {
        ("Issue A", "candidate A"): {
            "match": True,
            "confidence": 0.91,
            "reasoning": "same",
        }
    }

    class Judge:
        async def match_comment(self, golden: str, candidate: str) -> dict[str, Any]:
            return responses.get(
                (golden, candidate),
                {"match": False, "confidence": 0.1, "reasoning": "different"},
            )

    result = await evaluate_review(
        Judge(),
        [
            {"comment": "Issue A", "severity": "High", "category": "bug"},
            {"comment": "Issue B", "severity": "Low", "category": "test"},
        ],
        ["candidate A", "candidate A duplicate", "unrelated"],
        [[0, 1], [2]],
    )

    assert result["tp"] == 1
    assert result["fp"] == 1
    assert result["fn"] == 1
    assert result["precision"] == pytest.approx(1 / 3)
    assert result["recall"] == pytest.approx(1 / 2)
    assert result["false_positives"] == [{"candidate": "unrelated"}]
    assert result["true_positives"][0]["golden_comment"] == "Issue A"
    assert len(result["pair_matches"]) == 6
    assert result["pair_matches"][0]["golden_index"] == 0
    assert result["pair_matches"][0]["candidate_index"] == 0


@pytest.mark.asyncio
async def test_duplicate_golden_and_candidate_texts_keep_distinct_indexes() -> None:
    class Judge:
        async def match_comment(self, _golden: str, _candidate: str) -> dict[str, Any]:
            return {"match": True, "confidence": 0.9, "reasoning": "same"}

    result = await evaluate_review(
        Judge(),
        [
            {"comment": "same golden", "severity": "High", "category": "bug"},
            {"comment": "same golden", "severity": "Low", "category": "test"},
        ],
        ["same candidate", "same candidate"],
    )

    assert result["total_golden"] == 2
    assert result["total_candidates"] == 2
    assert (result["tp"], result["fp"], result["fn"]) == (2, 1, 0)
    assert len(result["true_positives"]) == 2
    assert result["false_positives"] == [{"candidate": "same candidate"}]
    assert {(pair["golden_index"], pair["candidate_index"]) for pair in result["pair_matches"]} == {
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    }


@pytest.mark.asyncio
async def test_martian_judge_uses_one_global_concurrency_limit() -> None:
    active = 0
    maximum = 0
    payloads: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        payloads.append(json.loads(request.content))
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"reasoning":"same","match":true,"confidence":0.9}'}}
                ]
            },
        )

    secret = "sk-judge-test-secret"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        judge = MartianJudge(
            JudgeConfig(
                model="anthropic/judge",
                api_key=secret,
                api_base="https://gateway.example/v1",
                concurrency=3,
            ),
            http_client=client,
        )
        results = await asyncio.gather(
            *(judge.match_comment(f"golden {index}", f"candidate {index}") for index in range(12))
        )

    assert maximum == 3
    assert all(result["match"] for result in results)
    assert all(payload["model"] == "anthropic/judge" for payload in payloads)
    assert all(payload["temperature"] == 0.0 for payload in payloads)
    assert payloads[0]["messages"][0]["role"] == "system"
    assert JUDGE_PROMPT.splitlines()[0] in payloads[0]["messages"][1]["content"]
    assert secret not in repr(judge.config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", True),
        ("concurrency", 1.5),
        ("concurrency", 0),
        ("call_timeout_seconds", float("nan")),
        ("call_timeout_seconds", float("inf")),
        ("call_timeout_seconds", True),
        ("max_attempts", True),
        ("max_attempts", 1.5),
        ("max_attempts", 0),
    ],
)
def test_judge_config_requires_typed_finite_positive_bounds(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match="positive"):
        JudgeConfig(model="judge", api_key="secret", **{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_content",
    [
        '{"reasoning":"different","match":"false","confidence":0.9}',
        '{"reasoning":"same","match":true,"confidence":"0.9"}',
        ('{"reasoning":"same","match":true,"match":false,"confidence":0.9}'),
        ('{"reasoning":"same","match":true,"confidence":0.9,"confidence":0.1}'),
    ],
)
async def test_martian_judge_retries_string_typed_semantics(
    invalid_content: str,
) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = (
            invalid_content
            if calls == 1
            else '{"reasoning":"different","match":false,"confidence":0.1}'
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MartianJudge(
            JudgeConfig(model="judge", api_key="secret", max_attempts=2),
            http_client=client,
        ).match_comment("golden", "candidate")

    assert calls == 2
    assert result["match"] is False
    assert result["confidence"] == 0.1
    assert result["attempt_count"] == 2
    assert any(
        marker in result["retry_errors"][0]
        for marker in ("must be", "strict JSON", "duplicate JSON key")
    )


@pytest.mark.asyncio
async def test_martian_judge_fails_closed_after_semantic_retries_are_exhausted() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"reasoning":"different","match":"false","confidence":0.9}'
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MartianJudge(
            JudgeConfig(model="judge", api_key="secret", max_attempts=2),
            http_client=client,
        ).match_comment("golden", "candidate")

    assert calls == 2
    assert result["error"] == "Judge response failed semantic validation"
    assert result["attempt_count"] == 2
    assert len(result["retry_errors"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [
        {"reasoning": "different", "match": "false", "confidence": 0.9},
        {"reasoning": "same", "match": True, "confidence": "0.9"},
        {"reasoning": "same", "match": True, "confidence": 1.1},
    ],
)
async def test_evaluate_review_fails_closed_for_malformed_semantics(
    malformed: dict[str, Any],
) -> None:
    class Judge:
        async def match_comment(self, _golden: str, _candidate: str) -> dict[str, Any]:
            return malformed

    result = await evaluate_review(
        Judge(),
        [{"comment": "golden", "severity": "High", "category": "bug"}],
        ["candidate"],
    )

    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 1)
    assert result["errors_count"] == 1
    assert result["pair_matches"][0]["match"] is False
    assert result["pair_matches"][0]["confidence"] == 0.0


@pytest.mark.asyncio
async def test_runner_judges_tools_together_checkpoints_and_resumes(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    model_dir = results_dir / "anthropic_judge"
    model_dir.mkdir(parents=True)
    golden_url = "https://github.com/upstream/repo/pull/1"
    benchmark = {
        golden_url: {
            "golden_comments": [
                {"comment": "the cache is stale", "severity": "High", "category": "bug"}
            ],
            "reviews": [
                {"tool": "tool-a", "repo_name": "repo", "pr_url": "fixture-a"},
                {"tool": "tool-b", "repo_name": "repo", "pr_url": "fixture-b"},
            ],
        }
    }
    candidates = {
        golden_url: {
            "tool-a": [{"text": "the cache is stale"}],
            "tool-b": [{"text": "rename this variable"}],
        }
    }
    groups = {golden_url: {"tool-a": [[0]], "tool-b": [[0]]}}
    (results_dir / "benchmark_data.json").write_text(json.dumps(benchmark), encoding="utf-8")
    (model_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (model_dir / "dedup_groups.json").write_text(json.dumps(groups), encoding="utf-8")

    class Judge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def match_comment(self, golden: str, candidate: str) -> dict[str, Any]:
            self.calls.append((golden, candidate))
            return {
                "match": golden == candidate,
                "confidence": 0.9,
                "reasoning": "exact" if golden == candidate else "different",
            }

    first_judge = Judge()
    first = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=first_judge,
    )

    assert first["evaluated"] == 2
    assert first["resumed"] == 0
    assert len(first_judge.calls) == 2
    assert first["metrics"]["tool-a"]["f1"] == 1.0
    assert first["metrics"]["tool-b"]["f1"] == 0.0
    evaluations = model_dir / "evaluations.json"
    assert evaluations.is_file()
    assert json.loads(evaluations.read_text(encoding="utf-8"))[golden_url]["tool-a"]["tp"] == 1

    second_judge = Judge()
    second = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=second_judge,
    )
    assert second["evaluated"] == 0
    assert second["resumed"] == 2
    assert second_judge.calls == []

    forced_judge = Judge()
    forced = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        tools=["tool-a"],
        force=True,
        judge=forced_judge,
    )
    assert forced["evaluated"] == 1
    assert len(forced_judge.calls) == 1


def _single_case_fixture(tmp_path: Path, candidate_text: str) -> tuple[Path, Path, str]:
    results_dir = tmp_path / "results"
    model_dir = results_dir / "anthropic_judge"
    model_dir.mkdir(parents=True, exist_ok=True)
    golden_url = "https://github.com/upstream/repo/pull/1"
    benchmark = {
        golden_url: {
            "golden_comments": [
                {"comment": "the cache is stale", "severity": "High", "category": "bug"}
            ],
            "reviews": [{"tool": "tool-a", "repo_name": "repo", "pr_url": "fixture-a"}],
        }
    }
    candidates = {golden_url: {"tool-a": [{"text": candidate_text}]}}
    groups = {golden_url: {"tool-a": [[0]]}}
    (results_dir / "benchmark_data.json").write_text(json.dumps(benchmark), encoding="utf-8")
    (model_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (model_dir / "dedup_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    return results_dir, model_dir, golden_url


class _RecordingJudge:
    def __init__(self, *, error: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error

    async def match_comment(self, golden: str, candidate: str) -> dict[str, Any]:
        self.calls.append((golden, candidate))
        if self.error:
            return {"error": "boom", "attempt_count": 1, "retry_errors": []}
        return {"match": golden == candidate, "confidence": 0.9, "reasoning": "r"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("judge_concurrency", True),
        ("review_concurrency", 0),
        ("review_concurrency", 1.5),
        ("call_timeout_seconds", float("inf")),
        ("review_timeout_seconds", float("nan")),
        ("max_attempts", 1.5),
    ],
)
async def test_runner_rejects_unbounded_library_limits_before_state_mutation(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    results_dir, model_dir, _golden_url = _single_case_fixture(tmp_path, "candidate")

    with pytest.raises(ValueError, match="positive"):
        await run_codereviewbench_judge(
            results_dir=results_dir,
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            judge=_RecordingJudge(),
            **{field: value},
        )

    assert not (model_dir / "evaluations.json").exists()
    assert not (model_dir / ".evaluations.json.lock").exists()


@pytest.mark.asyncio
async def test_resume_is_bound_to_the_judged_candidate_content(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    first = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    assert first["evaluated"] == 1

    # A same-tool re-export replaces the candidates; the old evaluation must
    # not be silently resumed against content it never judged.
    replaced = {golden_url: {"tool-a": [{"text": "an entirely different finding"}]}}
    (model_dir / "candidates.json").write_text(json.dumps(replaced), encoding="utf-8")
    rejudge = _RecordingJudge()
    second = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=rejudge,
    )
    assert second["evaluated"] == 1
    assert second["resumed"] == 0
    assert rejudge.calls == [("the cache is stale", "an entirely different finding")]
    stored = json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8"))
    assert stored[golden_url]["tool-a"]["false_positives"] == [
        {"candidate": "an entirely different finding"}
    ]

    third = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    assert third["evaluated"] == 0
    assert third["resumed"] == 1


@pytest.mark.asyncio
async def test_changed_input_timeout_cannot_leave_stale_evaluation_row(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    replaced = {golden_url: {"tool-a": [{"text": "replacement candidate"}]}}
    (model_dir / "candidates.json").write_text(json.dumps(replaced), encoding="utf-8")

    class NeverJudge:
        async def match_comment(self, _golden: str, _candidate: str) -> dict[str, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    report = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        review_timeout_seconds=0.01,
        judge=NeverJudge(),
    )

    assert report["evaluated"] == 0
    assert report["timed_out"] == 1
    assert report["metrics"] == {}
    stored = json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8"))
    assert golden_url not in stored or "tool-a" not in stored[golden_url]


@pytest.mark.asyncio
async def test_subset_reexport_durably_removes_same_tool_retired_cases(tmp_path: Path) -> None:
    results_dir, model_dir, first_url = _single_case_fixture(tmp_path, "the cache is stale")
    second_url = "https://github.com/upstream/repo/pull/2"
    benchmark_path = results_dir / "benchmark_data.json"
    candidates_path = model_dir / "candidates.json"
    groups_path = model_dir / "dedup_groups.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    groups = json.loads(groups_path.read_text(encoding="utf-8"))
    benchmark[second_url] = {
        "golden_comments": [
            {"comment": "the second cache is stale", "severity": "High", "category": "bug"}
        ],
        "reviews": [{"tool": "tool-a", "repo_name": "repo", "pr_url": "fixture-b"}],
    }
    candidates[second_url] = {"tool-a": [{"text": "the second cache is stale"}]}
    groups[second_url] = {"tool-a": [[0]]}
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    groups_path.write_text(json.dumps(groups), encoding="utf-8")
    first = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    assert first["evaluated"] == 2

    for path in (benchmark_path, candidates_path, groups_path):
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop(second_url)
        path.write_text(json.dumps(value), encoding="utf-8")
    resumed = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )

    assert resumed["evaluated"] == 0
    assert resumed["resumed"] == 1
    assert resumed["case_tool_population"] == 1
    stored = json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8"))
    assert set(stored) == {first_url}


@pytest.mark.asyncio
async def test_judge_waits_for_one_consistent_export_snapshot(tmp_path: Path) -> None:
    results_dir, _model_dir, _golden_url = _single_case_fixture(tmp_path, "old issue")
    benchmark_path = results_dir / "benchmark_data.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    first_url = next(iter(benchmark))
    benchmark[first_url]["golden_comments"][0]["comment"] = "old issue"
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    finish = context.Event()
    process = context.Process(
        target=_run_paused_export_process,
        args=(str(results_dir), ready, finish),
    )
    process.start()
    try:
        assert await asyncio.to_thread(ready.wait, 10)
        judging = asyncio.create_task(
            run_codereviewbench_judge(
                results_dir=results_dir,
                judge_model="anthropic/judge",
                api_key="unused-in-test",
                judge=_RecordingJudge(),
            )
        )
        await asyncio.sleep(0.1)
        assert not judging.done()
        finish.set()
        report = await asyncio.wait_for(judging, 10)
        assert report["metrics"]["tool-a"]["f1"] == 1.0
    finally:
        finish.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


@pytest.mark.asyncio
async def test_judge_rejects_first_export_phantom_rows_without_metadata(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "candidate")
    native_tool = "bugbunny-balanced-model-012345abcdef"
    benchmark_path = results_dir / "benchmark_data.json"
    candidates_path = model_dir / "candidates.json"
    groups_path = model_dir / "dedup_groups.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark[golden_url]["reviews"][0]["tool"] = native_tool
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    candidates[golden_url][native_tool] = candidates[golden_url].pop("tool-a")
    groups = json.loads(groups_path.read_text(encoding="utf-8"))
    groups[golden_url][native_tool] = groups[golden_url].pop("tool-a")
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    candidates_path.write_text(json.dumps(candidates), encoding="utf-8")
    groups_path.write_text(json.dumps(groups), encoding="utf-8")

    with pytest.raises(JudgeError, match="without a committed manifest/index"):
        await run_codereviewbench_judge(
            results_dir=results_dir,
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            tools=[native_tool],
            judge=_RecordingJudge(),
        )


@pytest.mark.asyncio
async def test_error_degraded_records_are_rejudged_not_resumed(tmp_path: Path) -> None:
    results_dir, _model_dir, _golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    errored = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(error=True),
    )
    assert errored["metrics"]["tool-a"]["errors"] == 1

    clean_judge = _RecordingJudge()
    clean = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=clean_judge,
    )
    assert clean["evaluated"] == 1
    assert clean["resumed"] == 0
    assert clean_judge.calls != []
    assert clean["metrics"]["tool-a"]["errors"] == 0


@pytest.mark.asyncio
async def test_reported_metrics_cover_only_the_current_tool_population(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    stale = {
        golden_url: {
            "tool-retired": {"tp": 0, "fp": 9, "fn": 9, "errors_count": 0, "skipped": False}
        },
        "https://github.com/upstream/repo/pull/retired": {
            "tool-a": {"tp": 0, "fp": 9, "fn": 9, "errors_count": 0, "skipped": False}
        },
    }
    (model_dir / "evaluations.json").write_text(json.dumps(stale), encoding="utf-8")
    report = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    assert "tool-retired" not in report["metrics"]
    assert report["case_tool_population"] == 1
    assert report["metrics"]["tool-a"]["reviews"] == 1
    assert report["metrics"]["tool-a"]["f1"] == 1.0


@pytest.mark.asyncio
async def test_resume_binds_full_goldens_and_versioned_judge_identity(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )

    benchmark_path = results_dir / "benchmark_data.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark[golden_url]["golden_comments"][0]["severity"] = "Critical"
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    changed_golden = _RecordingJudge()
    second = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=changed_golden,
    )
    assert second["evaluated"] == 1
    assert changed_golden.calls != []

    # These names deliberately collide under sanitize_model_name, so only the
    # stored exact model identity can prevent a stale resume.
    changed_model = _RecordingJudge()
    third = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic_judge",
        api_key="unused-in-test",
        judge=changed_model,
    )
    assert third["evaluated"] == 1
    assert third["judge_identity_sha256"] != second["judge_identity_sha256"]

    changed_config = _RecordingJudge()
    fourth = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic_judge",
        api_key="unused-in-test",
        call_timeout_seconds=31,
        judge=changed_config,
    )
    assert fourth["evaluated"] == 1
    assert fourth["judge_identity_sha256"] != third["judge_identity_sha256"]

    changed_review_timeout = _RecordingJudge()
    fifth = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic_judge",
        api_key="unused-in-test",
        call_timeout_seconds=31,
        review_timeout_seconds=1801,
        judge=changed_review_timeout,
    )
    assert fifth["evaluated"] == 1
    assert fifth["judge_identity_sha256"] != fourth["judge_identity_sha256"]

    resumed = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic_judge",
        api_key="unused-in-test",
        call_timeout_seconds=31,
        review_timeout_seconds=1801,
        judge=_RecordingJudge(),
    )
    assert resumed["evaluated"] == 0
    assert resumed["resumed"] == 1
    stored = json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8"))
    assert stored[golden_url]["tool-a"]["judge_identity_sha256"] == fifth["judge_identity_sha256"]


@pytest.mark.asyncio
async def test_explicitly_requested_missing_tool_is_rejected(tmp_path: Path) -> None:
    results_dir, model_dir, _golden_url = _single_case_fixture(tmp_path, "the cache is stale")

    with pytest.raises(JudgeError, match=r"absent.*tool-missing"):
        await run_codereviewbench_judge(
            results_dir=results_dir,
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            tools=["tool-missing"],
            judge=_RecordingJudge(),
        )

    assert not (model_dir / "evaluations.json").exists()


@pytest.mark.asyncio
async def test_duplicate_case_tool_reviews_are_rejected_before_judging(tmp_path: Path) -> None:
    results_dir, model_dir, golden_url = _single_case_fixture(tmp_path, "the cache is stale")
    benchmark_path = results_dir / "benchmark_data.json"
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    benchmark[golden_url]["reviews"].append(dict(benchmark[golden_url]["reviews"][0]))
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")

    with pytest.raises(JudgeError, match="duplicates review"):
        await run_codereviewbench_judge(
            results_dir=results_dir,
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            judge=_RecordingJudge(),
        )

    assert not (model_dir / "evaluations.json").exists()


def test_concurrent_processes_cannot_overwrite_each_others_evaluation_rows(
    tmp_path: Path,
) -> None:
    results_dir = tmp_path / "results"
    model_dir = results_dir / "anthropic_judge"
    model_dir.mkdir(parents=True)
    golden_url = "https://github.com/upstream/repo/pull/1"
    benchmark = {
        golden_url: {
            "golden_comments": [
                {"comment": "the cache is stale", "severity": "High", "category": "bug"}
            ],
            "reviews": [
                {"tool": "tool-a", "repo_name": "repo", "pr_url": "fixture-a"},
                {"tool": "tool-b", "repo_name": "repo", "pr_url": "fixture-b"},
            ],
        }
    }
    candidates = {
        golden_url: {
            "tool-a": [{"text": "the cache is stale"}],
            "tool-b": [{"text": "different"}],
        }
    }
    (results_dir / "benchmark_data.json").write_text(json.dumps(benchmark), encoding="utf-8")
    (model_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (model_dir / "dedup_groups.json").write_text("{}", encoding="utf-8")
    evaluations_file = model_dir / "shared-evaluations.json"

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_run_concurrent_judge_process,
            args=(str(results_dir), str(evaluations_file), tool, start),
        )
        for tool in ("tool-a", "tool-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
    try:
        assert [process.exitcode for process in processes] == [0, 0]
        stored = json.loads(evaluations_file.read_text(encoding="utf-8"))
        assert set(stored[golden_url]) == {"tool-a", "tool-b"}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_judge_result_overflowing_integer_confidence_is_rejected_not_crash():
    # math.isfinite(10**400) itself raises OverflowError, so a huge integer
    # confidence must be routed through the overflow-safe check and become a
    # semantic validation error inside the retry taxonomy.
    from bugbunny.judge import _validate_judge_result

    for confidence in (10**400, -(10**400)):
        with pytest.raises(Exception, match="finite"):
            _validate_judge_result(
                {"reasoning": "text", "match": True, "confidence": confidence},
                require_exact_keys=True,
            )


def test_aggregate_metrics_reports_both_conventions_and_degradation() -> None:
    from bugbunny.judge import EvaluationState, aggregate_metrics

    state = EvaluationState(
        completed={
            "https://example.com/pull/1": {
                "tool-a": {
                    # 1 candidate matched 4 goldens: upstream micro precision
                    # exceeds any proportion of candidates.
                    "tp": 4,
                    "fp": 0,
                    "fn": 0,
                    "errors_count": 0,
                    "total_candidates": 1,
                    "total_golden": 4,
                }
            },
            "https://example.com/pull/2": {
                "tool-a": {
                    # Zero candidates: macro precision counts this case as 0,
                    # micro pooling silently drops it from the denominator.
                    "tp": 0,
                    "fp": 0,
                    "fn": 2,
                    "errors_count": 1,
                    "total_candidates": 0,
                    "total_golden": 2,
                }
            },
        }
    )
    metrics = aggregate_metrics(state)
    metric = metrics["tool-a"]
    # Upstream-faithful micro pooling.
    assert metric["precision"] == 1.0
    assert metric["recall"] == 4 / 6
    # Paper-convention macro averages weight both cases equally.
    assert metric["macro_precision"] == 0.5
    assert metric["macro_recall"] == 0.5
    assert metric["candidate_match_rate"] == 1.0
    assert metric["error_degraded"] is True


def test_judge_phantom_detection_catches_custom_tool_prefixes() -> None:
    from bugbunny.judge import _EXPORTED_TOOL_SHAPE

    assert _EXPORTED_TOOL_SHAPE.fullmatch("bugbunny-balanced-openai-gpt-a1b2c3d4e5f6")
    # A crash before the first manifest commit under a custom tool= name must
    # be flagged as a phantom exactly like the default-prefixed one.
    assert _EXPORTED_TOOL_SHAPE.fullmatch("mytool-balanced-openai-gpt-a1b2c3d4e5f6")
    assert not _EXPORTED_TOOL_SHAPE.fullmatch("greptile")
    assert not _EXPORTED_TOOL_SHAPE.fullmatch("bugbunny-balanced-short-a1b2")


@pytest.mark.asyncio
async def test_runner_settles_all_items_before_raising_a_persist_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fail-fast gather abandoned in-flight sibling evaluations whose
    # to_thread checkpoint writes could land after the evaluations lease was
    # released. Every item must settle before the failure propagates.
    import bugbunny.judge as judge_module

    results_dir = tmp_path / "results"
    model_dir = results_dir / "anthropic_judge"
    model_dir.mkdir(parents=True)
    golden_url = "https://github.com/upstream/repo/pull/1"
    benchmark = {
        golden_url: {
            "golden_comments": [
                {"comment": "the cache is stale", "severity": "High", "category": "bug"}
            ],
            "reviews": [
                {"tool": "tool-a", "repo_name": "repo", "pr_url": "fixture-a"},
                {"tool": "tool-b", "repo_name": "repo", "pr_url": "fixture-b"},
            ],
        }
    }
    candidates = {
        golden_url: {
            "tool-a": [{"text": "the cache is stale"}],
            "tool-b": [{"text": "the cache is stale"}],
        }
    }
    groups = {golden_url: {"tool-a": [[0]], "tool-b": [[0]]}}
    (results_dir / "benchmark_data.json").write_text(json.dumps(benchmark), encoding="utf-8")
    (model_dir / "candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
    (model_dir / "dedup_groups.json").write_text(json.dumps(groups), encoding="utf-8")

    class Judge:
        async def match_comment(self, golden: str, candidate: str) -> dict[str, Any]:
            return {"match": True, "confidence": 0.9, "reasoning": "exact"}

    real_write = judge_module.atomic_write_text
    failures = {"remaining": 1}

    def failing_write(path: Path, value: str) -> None:
        if failures["remaining"] > 0:
            failures["remaining"] -= 1
            raise OSError("disk full")
        real_write(path, value)

    monkeypatch.setattr(judge_module, "atomic_write_text", failing_write)

    with pytest.raises(OSError, match="disk full"):
        await run_codereviewbench_judge(
            results_dir=results_dir,
            judge_model="anthropic/judge",
            api_key="unused-in-test",
            judge=Judge(),
        )

    # Both rows were evaluated and the surviving checkpoint write landed
    # before the failure propagated out of the runner.
    evaluations = json.loads((model_dir / "evaluations.json").read_text(encoding="utf-8"))
    assert set(evaluations.get(golden_url, {})) == {"tool-a", "tool-b"}
