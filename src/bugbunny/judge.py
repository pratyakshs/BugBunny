"""Concurrent, restartable CodeReviewBench Step 3 evaluation.

The matching prompt and metric reduction intentionally retain the benchmark's
semantics.  Only scheduling, transport pooling, and checkpoint durability are
different: every comparison shares one bounded Martian request queue and each
completed pull-request/tool result is committed atomically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bugbunny.benchmark import sanitize_model_name
from bugbunny.gateway import MARTIAN_API_BASE
from bugbunny.util import atomic_write_json, load_json

JUDGE_PROMPT = """You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}}"""

SYSTEM_PROMPT = "You are a precise code review evaluator. Always respond with valid JSON."


class JudgeError(RuntimeError):
    """A safe-to-display judge configuration or input error."""


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    api_key: str = field(repr=False, compare=False)
    api_base: str = field(default=MARTIAN_API_BASE, repr=False)
    concurrency: int = 20
    call_timeout_seconds: float = 30
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("judge model must not be empty")
        if not self.api_key:
            raise ValueError("Martian API key is required for benchmark judging")
        if self.concurrency <= 0:
            raise ValueError("judge concurrency must be positive")
        if self.call_timeout_seconds <= 0:
            raise ValueError("judge call timeout must be positive")
        if self.max_attempts <= 0:
            raise ValueError("judge max attempts must be positive")


@dataclass
class EvaluationState:
    """CodeReviewBench-compatible evaluation records with atomic persistence."""

    completed: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def is_done(self, golden_url: str, tool: str) -> bool:
        result = self.completed.get(golden_url, {}).get(tool)
        return isinstance(result, Mapping) and result.get("errors_count", 0) == 0

    def mark_done(self, golden_url: str, tool: str, result: dict[str, Any]) -> None:
        self.completed.setdefault(golden_url, {})[tool] = result

    def clear_tools(self, tools: set[str] | None) -> None:
        if tools is None:
            self.completed.clear()
            return
        for per_tool in self.completed.values():
            for tool in tools:
                per_tool.pop(tool, None)

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.completed)

    @classmethod
    def load(cls, path: Path) -> EvaluationState:
        if not path.is_file():
            return cls()
        try:
            value = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise JudgeError(f"cannot read evaluations file {path}: {exc}") from exc
        if not isinstance(value, Mapping):
            raise JudgeError(f"evaluations file must contain an object: {path}")
        completed: dict[str, dict[str, dict[str, Any]]] = {}
        for golden_url, per_tool in value.items():
            if not isinstance(golden_url, str) or not isinstance(per_tool, Mapping):
                raise JudgeError(f"evaluations file has invalid entries: {path}")
            completed[golden_url] = {}
            for tool, result in per_tool.items():
                if not isinstance(tool, str) or not isinstance(result, Mapping):
                    raise JudgeError(f"evaluations file has invalid tool results: {path}")
                completed[golden_url][tool] = dict(result)
        return cls(completed)


class MartianJudge:
    """OpenAI-compatible Martian judge with one global request limiter."""

    def __init__(
        self,
        config: JudgeConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._http_client = http_client
        self._owns_http_client = False

    async def __aenter__(self) -> MartianJudge:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.call_timeout_seconds),
                limits=httpx.Limits(
                    max_connections=max(100, self.config.concurrency),
                    max_keepalive_connections=max(20, self.config.concurrency),
                ),
                follow_redirects=False,
                http2=False,
            )
            self._owns_http_client = True
        return self._http_client

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            self._owns_http_client = False

    def _safe_error(self, exc: BaseException) -> str:
        message = f"{type(exc).__name__}: {exc}"
        for secret in (self.config.api_key, self.config.api_base):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message[:2_000]

    async def call_llm(self, prompt: str) -> dict[str, Any]:
        endpoint = f"{self.config.api_base.rstrip('/')}/chat/completions"
        request = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        retry_errors: list[str] = []
        for attempt in range(self.config.max_attempts):
            try:
                async with self._semaphore:
                    response = await asyncio.wait_for(
                        self._client().post(
                            endpoint,
                            headers={
                                "Authorization": f"Bearer {self.config.api_key}",
                                "Content-Type": "application/json",
                            },
                            json=request,
                        ),
                        timeout=self.config.call_timeout_seconds,
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("judge response must be a JSON object")
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise ValueError("judge response has no choices")
                first = choices[0]
                if not isinstance(first, Mapping) or not isinstance(first.get("message"), Mapping):
                    raise ValueError("judge response has no message")
                content = first["message"].get("content")
                if not isinstance(content, str):
                    raise ValueError("judge response content is not text")
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("judge result must be a JSON object")
                result["attempt_count"] = attempt + 1
                result["retry_errors"] = retry_errors
                return result
            except (TimeoutError, httpx.TimeoutException) as exc:
                retry_errors.append(self._safe_error(exc))
                if attempt == self.config.max_attempts - 1:
                    return {
                        "error": (f"Timed out after {self.config.call_timeout_seconds:g}s"),
                        "attempt_count": attempt + 1,
                        "retry_errors": retry_errors,
                    }
                await asyncio.sleep(2**attempt)
            except json.JSONDecodeError as exc:
                retry_errors.append(self._safe_error(exc))
                if attempt == self.config.max_attempts - 1:
                    return {
                        "error": "JSON parse failed",
                        "attempt_count": attempt + 1,
                        "retry_errors": retry_errors,
                    }
                await asyncio.sleep(1)
            except Exception as exc:
                retry_errors.append(self._safe_error(exc))
                if attempt == self.config.max_attempts - 1:
                    return {
                        "error": self._safe_error(exc),
                        "attempt_count": attempt + 1,
                        "retry_errors": retry_errors,
                    }
                lowered = str(exc).lower()
                if "429" in lowered or "rate" in lowered or "too many" in lowered:
                    await asyncio.sleep(min(10 * (3**attempt), 120))
                else:
                    await asyncio.sleep(2**attempt)
        return {
            "error": "Max retries exceeded",
            "attempt_count": self.config.max_attempts,
            "retry_errors": retry_errors,
        }

    async def match_comment(self, golden_comment: str, candidate: str) -> dict[str, Any]:
        return await self.call_llm(
            JUDGE_PROMPT.format(golden_comment=golden_comment, candidate=candidate)
        )


def get_candidates(
    review: Mapping[str, Any],
    all_candidates: Mapping[str, Any],
    golden_url: str,
) -> list[str]:
    """Prefer exported candidates and retain the benchmark's raw-comment fallback."""

    tool = review["tool"]
    per_url = all_candidates.get(golden_url)
    if isinstance(per_url, Mapping) and isinstance(per_url.get(tool), list):
        return [
            candidate["text"]
            for candidate in per_url[tool]
            if isinstance(candidate, Mapping)
            and isinstance(candidate.get("text"), str)
            and candidate["text"]
        ]
    comments = review.get("review_comments", [])
    if not isinstance(comments, list):
        return []
    return [
        comment["body"]
        for comment in comments
        if isinstance(comment, Mapping) and isinstance(comment.get("body"), str) and comment["body"]
    ]


def _build_sibling_map(
    candidates: list[str], groups: list[list[int]] | None
) -> dict[str, set[str]]:
    if not groups:
        return {}
    sibling_map: dict[str, set[str]] = {}
    for group in groups:
        group_texts = {candidates[index] for index in group if index < len(candidates)}
        for index in group:
            if index < len(candidates):
                sibling_map[candidates[index]] = group_texts - {candidates[index]}
    return sibling_map


async def evaluate_review(
    judge: Any,
    golden_comments: list[dict[str, Any]],
    candidates: list[str],
    dedup_groups: list[list[int]] | None = None,
) -> dict[str, Any]:
    """Match one review while preserving CodeReviewBench metric reduction order."""

    if not golden_comments:
        return {"skipped": True, "reason": "No golden comments"}
    if not candidates:
        return {
            "skipped": False,
            "true_positives": [],
            "false_positives": [],
            "false_negatives": [
                {
                    "golden_comment": comment["comment"],
                    "severity": comment.get("severity"),
                    "category": comment.get("category"),
                }
                for comment in golden_comments
            ],
            "errors": [],
            "pair_matches": [],
            "total_candidates": 0,
            "total_golden": len(golden_comments),
            "tp": 0,
            "fp": 0,
            "fn": len(golden_comments),
            "errors_count": 0,
            "precision": 0.0,
            "recall": 0.0,
        }

    tasks = []
    task_meta: list[dict[str, Any]] = []
    for golden_index, golden in enumerate(golden_comments):
        for candidate_index, candidate in enumerate(candidates):
            tasks.append(judge.match_comment(golden["comment"], candidate))
            task_meta.append(
                {
                    "golden": golden["comment"],
                    "golden_index": golden_index,
                    "golden_severity": golden.get("severity"),
                    "candidate": candidate,
                    "candidate_index": candidate_index,
                }
            )
    results = await asyncio.gather(*tasks, return_exceptions=True)

    golden_matched = {
        comment["comment"]: {
            "severity": comment.get("severity"),
            "category": comment.get("category"),
            "matched": False,
            "best_confidence": 0.0,
            "matched_candidate": None,
        }
        for comment in golden_comments
    }
    candidate_matched = dict.fromkeys(candidates, False)
    sibling_map = _build_sibling_map(candidates, dedup_groups)
    errors = []
    pair_matches: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        metadata = task_meta[index]
        golden = metadata["golden"]
        candidate = metadata["candidate"]
        if isinstance(result, BaseException):
            errors.append({"golden": golden, "candidate": candidate, "error": str(result)})
            pair_matches.append(
                {
                    **metadata,
                    "match": False,
                    "confidence": 0.0,
                    "error": f"{type(result).__name__}: {result}",
                    "attempt_count": None,
                    "retry_errors": [],
                }
            )
            continue
        pair_matches.append(
            {
                **metadata,
                "match": bool(result.get("match")),
                "confidence": result.get("confidence", 0),
                "reasoning": result.get("reasoning"),
                "error": result.get("error"),
                "attempt_count": result.get("attempt_count", 1),
                "retry_errors": result.get("retry_errors", []),
            }
        )
        if result.get("error"):
            errors.append({"golden": golden, "candidate": candidate, "error": result["error"]})
            continue
        confidence = result.get("confidence", 0)
        if result.get("match") and confidence > golden_matched[golden]["best_confidence"]:
            golden_matched[golden]["matched"] = True
            golden_matched[golden]["best_confidence"] = confidence
            golden_matched[golden]["matched_candidate"] = candidate
            golden_matched[golden]["reasoning"] = result.get("reasoning")
            candidate_matched[candidate] = True
            for sibling in sibling_map.get(candidate, set()):
                candidate_matched[sibling] = True

    true_positives = []
    false_negatives = []
    for golden, information in golden_matched.items():
        if information["matched"]:
            true_positives.append(
                {
                    "golden_comment": golden,
                    "severity": information["severity"],
                    "category": information["category"],
                    "matched_candidate": information["matched_candidate"],
                    "confidence": information["best_confidence"],
                    "reasoning": information.get("reasoning"),
                }
            )
        else:
            false_negatives.append(
                {
                    "golden_comment": golden,
                    "severity": information["severity"],
                    "category": information["category"],
                }
            )
    false_positives = [
        {"candidate": candidate} for candidate, matched in candidate_matched.items() if not matched
    ]
    total_candidates = len(candidates)
    total_golden = len(golden_comments)
    true_positive_count = len(true_positives)
    return {
        "skipped": False,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "errors": errors,
        "pair_matches": pair_matches,
        "total_candidates": total_candidates,
        "total_golden": total_golden,
        "tp": true_positive_count,
        "fp": len(false_positives),
        "fn": len(false_negatives),
        "errors_count": len(errors),
        "precision": true_positive_count / total_candidates if total_candidates else 0.0,
        "recall": true_positive_count / total_golden if total_golden else 0.0,
    }


def _load_object(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise JudgeError(f"required judge input is missing: {path}")
        return {}
    try:
        value = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise JudgeError(f"cannot read judge input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JudgeError(f"judge input must contain an object: {path}")
    return value


def aggregate_metrics(state: EvaluationState) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for per_tool in state.completed.values():
        for tool, result in per_tool.items():
            if result.get("skipped"):
                continue
            metric = totals.setdefault(tool, {"tp": 0, "fp": 0, "fn": 0, "errors": 0, "reviews": 0})
            metric["tp"] += result.get("tp", 0)
            metric["fp"] += result.get("fp", 0)
            metric["fn"] += result.get("fn", 0)
            metric["errors"] += result.get("errors_count", 0)
            metric["reviews"] += 1
    for metric in totals.values():
        denominator = metric["tp"] + metric["fp"]
        metric["precision"] = metric["tp"] / denominator if denominator else 0.0
        denominator = metric["tp"] + metric["fn"]
        metric["recall"] = metric["tp"] / denominator if denominator else 0.0
        denominator = metric["precision"] + metric["recall"]
        metric["f1"] = (
            2 * metric["precision"] * metric["recall"] / denominator if denominator else 0.0
        )
    return {tool: totals[tool] for tool in sorted(totals)}


async def run_codereviewbench_judge(
    *,
    results_dir: Path,
    judge_model: str,
    api_key: str,
    api_base: str = MARTIAN_API_BASE,
    tools: Sequence[str] | None = None,
    judge_concurrency: int = 20,
    review_concurrency: int = 10,
    call_timeout_seconds: float = 30,
    review_timeout_seconds: float = 1800,
    max_attempts: int = 5,
    force: bool = False,
    evaluations_file: Path | None = None,
    judge: Any | None = None,
) -> dict[str, Any]:
    """Evaluate all selected review/tool pairs through one shared judge queue."""

    root = results_dir.expanduser().resolve()
    benchmark_data = _load_object(root / "benchmark_data.json", required=True)
    model_dir = root / sanitize_model_name(judge_model)
    all_candidates = _load_object(model_dir / "candidates.json", required=False)
    all_dedup_groups = _load_object(model_dir / "dedup_groups.json", required=False)
    output_path = (
        evaluations_file.expanduser().resolve()
        if evaluations_file is not None
        else model_dir / "evaluations.json"
    )
    selected_tools = set(tools) if tools else None
    if selected_tools is not None and (not selected_tools or "" in selected_tools):
        raise JudgeError("--tool values must be non-empty")
    state = EvaluationState.load(output_path)
    if force:
        state.clear_tools(selected_tools)
        state.save(output_path)

    work_items: list[tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any], str]] = []
    skipped = 0
    for golden_url, raw_entry in benchmark_data.items():
        if not isinstance(golden_url, str) or not isinstance(raw_entry, Mapping):
            raise JudgeError("benchmark_data.json contains an invalid case")
        entry = dict(raw_entry)
        golden_comments = entry.get("golden_comments", [])
        reviews = entry.get("reviews", [])
        if not isinstance(golden_comments, list) or not isinstance(reviews, list):
            raise JudgeError(f"benchmark case has invalid reviews/comments: {golden_url}")
        for raw_review in reviews:
            if not isinstance(raw_review, Mapping) or not isinstance(raw_review.get("tool"), str):
                raise JudgeError(f"benchmark case has an invalid review: {golden_url}")
            review = dict(raw_review)
            tool = review["tool"]
            if selected_tools is not None and tool not in selected_tools:
                continue
            if not force and state.is_done(golden_url, tool):
                skipped += 1
                continue
            work_items.append((golden_url, entry, golden_comments, review, tool))

    # Start expensive reviews first. This changes only queue order; each
    # review's comparison order and metric reduction remain unchanged.
    def comparison_count(
        item: tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any], str],
    ) -> int:
        golden_url, _entry, golden_comments, review, _tool = item
        return len(golden_comments) * len(get_candidates(review, all_candidates, golden_url))

    work_items.sort(key=lambda item: (-comparison_count(item), item[0], item[4]))
    owns_judge = judge is None
    if judge is None:
        judge = MartianJudge(
            JudgeConfig(
                model=judge_model,
                api_key=api_key,
                api_base=api_base,
                concurrency=judge_concurrency,
                call_timeout_seconds=call_timeout_seconds,
                max_attempts=max_attempts,
            )
        )
    review_semaphore = asyncio.Semaphore(review_concurrency)
    state_lock = asyncio.Lock()
    evaluated = 0
    timed_out = 0

    async def evaluate_item(
        item: tuple[str, dict[str, Any], list[dict[str, Any]], dict[str, Any], str],
    ) -> None:
        nonlocal evaluated, timed_out
        golden_url, _entry, golden_comments, review, tool = item
        candidates = get_candidates(review, all_candidates, golden_url)
        per_url_groups = all_dedup_groups.get(golden_url)
        groups = per_url_groups.get(tool) if isinstance(per_url_groups, Mapping) else None
        if groups is not None and not isinstance(groups, list):
            raise JudgeError(f"dedup groups are invalid for {golden_url} / {tool}")
        try:
            async with review_semaphore:
                result = await asyncio.wait_for(
                    evaluate_review(judge, golden_comments, candidates, groups),
                    timeout=review_timeout_seconds,
                )
        except TimeoutError:
            async with state_lock:
                timed_out += 1
            return
        result["tool"] = tool
        result["repo_name"] = review.get("repo_name")
        result["pr_url"] = review.get("pr_url")
        async with state_lock:
            state.mark_done(golden_url, tool, result)
            await asyncio.to_thread(state.save, output_path)
            evaluated += 1

    try:
        await asyncio.gather(*(evaluate_item(item) for item in work_items))
    finally:
        if owns_judge:
            await judge.aclose()

    metrics = aggregate_metrics(state)
    return {
        "evaluations_file": str(output_path),
        "judge_model": judge_model,
        "evaluated": evaluated,
        "resumed": skipped,
        "timed_out": timed_out,
        "metrics": metrics,
    }


__all__ = [
    "JUDGE_PROMPT",
    "EvaluationState",
    "JudgeConfig",
    "JudgeError",
    "MartianJudge",
    "aggregate_metrics",
    "evaluate_review",
    "get_candidates",
    "run_codereviewbench_judge",
]
