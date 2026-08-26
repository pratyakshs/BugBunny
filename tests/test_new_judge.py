from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bugbunny.judge import (
    JUDGE_PROMPT,
    JudgeConfig,
    MartianJudge,
    evaluate_review,
    get_candidates,
    run_codereviewbench_judge,
)


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
            return responses.get((golden, candidate), {"match": False, "confidence": 0.1})

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
        }
    }
    (model_dir / "evaluations.json").write_text(json.dumps(stale), encoding="utf-8")
    report = await run_codereviewbench_judge(
        results_dir=results_dir,
        judge_model="anthropic/judge",
        api_key="unused-in-test",
        judge=_RecordingJudge(),
    )
    assert "tool-retired" not in report["metrics"]
    assert report["metrics"]["tool-a"]["f1"] == 1.0
