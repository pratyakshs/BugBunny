"""Concurrent, restartable CodeReviewBench Step 3 evaluation.

The matching prompt and metric reduction intentionally retain the benchmark's
semantics.  Only scheduling, transport pooling, and checkpoint durability are
different: every comparison shares one bounded Martian request queue and each
completed pull-request/tool result is committed atomically.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bugbunny.benchmark import sanitize_model_name, verify_codereviewbench_export_manifest
from bugbunny.build import (
    EXPORT_INDEX_SCHEMA_VERSION,
    implementation_identity,
)
from bugbunny.gateway import MARTIAN_API_BASE, strict_json_loads
from bugbunny.util import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    file_lock,
    is_finite_number,
    load_json,
    sha256_bytes,
    sha256_text,
)

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
JUDGE_IDENTITY_VERSION = "bugbunny-codereviewbench-judge-v2"
JUDGED_INPUTS_VERSION = "bugbunny-judged-inputs-v2"
_BUGBUNNY_TOOL_ID = re.compile(r"bugbunny-.+-[0-9a-f]{12}\Z")


class _JudgeResponseError(ValueError):
    """A syntactically valid but semantically invalid judge response."""


class JudgeError(RuntimeError):
    """A safe-to-display judge configuration or input error."""


def _require_positive_int(value: Any, *, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _require_positive_finite(value: Any, *, label: str) -> None:
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    api_key: str = field(repr=False, compare=False)
    api_base: str = field(default=MARTIAN_API_BASE, repr=False)
    concurrency: int = 20
    call_timeout_seconds: float = 30
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("judge model must not be empty")
        if not self.api_key:
            raise ValueError("Martian API key is required for benchmark judging")
        _require_positive_int(self.concurrency, label="judge concurrency")
        _require_positive_finite(self.call_timeout_seconds, label="judge call timeout")
        _require_positive_int(self.max_attempts, label="judge max attempts")


def judge_identity_payload(
    *,
    judge_model: str,
    api_base: str,
    call_timeout_seconds: float,
    review_timeout_seconds: float,
    max_attempts: int,
) -> dict[str, Any]:
    """Return every non-secret setting that can change a judge decision.

    Concurrency is deliberately excluded because it changes scheduling, not the
    request or retry contract. The API key is secret authentication material,
    not experiment identity.
    """

    if not isinstance(judge_model, str) or not judge_model.strip():
        raise ValueError("judge model must not be empty")
    if not isinstance(api_base, str) or not api_base.strip():
        raise ValueError("judge API base must not be empty")
    _require_positive_finite(call_timeout_seconds, label="judge call timeout")
    _require_positive_finite(review_timeout_seconds, label="judge review timeout")
    _require_positive_int(max_attempts, label="judge max attempts")
    return {
        "schema_version": JUDGE_IDENTITY_VERSION,
        "implementation": implementation_identity(),
        "model": judge_model,
        # Preserve backend identity without persisting a custom URL that may
        # accidentally contain userinfo or a sensitive query string.
        "api_base_sha256": sha256_text(api_base.rstrip("/")),
        "call_timeout_seconds": float(call_timeout_seconds),
        "review_timeout_seconds": float(review_timeout_seconds),
        "max_attempts": max_attempts,
        "temperature": 0.0,
        "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "judge_prompt_sha256": sha256_text(JUDGE_PROMPT),
    }


def validate_judge_identity_payload(
    value: Any,
    *,
    expected_model: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize a persisted judge identity for analysis."""

    expected_keys = {
        "schema_version",
        "implementation",
        "model",
        "api_base_sha256",
        "call_timeout_seconds",
        "review_timeout_seconds",
        "max_attempts",
        "temperature",
        "system_prompt_sha256",
        "judge_prompt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError("judge identity payload has an unsupported shape")
    model = value.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("judge identity model must not be empty")
    if expected_model is not None and model != expected_model:
        raise ValueError("judge identity model differs from the analyzed judge model")
    api_base_sha256 = value.get("api_base_sha256")
    if (
        not isinstance(api_base_sha256, str)
        or len(api_base_sha256) != 64
        or any(character not in "0123456789abcdef" for character in api_base_sha256)
    ):
        raise ValueError("judge identity API-base hash is invalid")
    _require_positive_finite(value.get("call_timeout_seconds"), label="judge call timeout")
    _require_positive_finite(value.get("review_timeout_seconds"), label="judge review timeout")
    _require_positive_int(value.get("max_attempts"), label="judge max attempts")
    temperature = value.get("temperature")
    if (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or temperature != 0
    ):
        raise ValueError("judge identity temperature must be zero")
    if (
        value.get("schema_version") != JUDGE_IDENTITY_VERSION
        or value.get("implementation") != implementation_identity()
        or value.get("system_prompt_sha256") != sha256_text(SYSTEM_PROMPT)
        or value.get("judge_prompt_sha256") != sha256_text(JUDGE_PROMPT)
    ):
        raise ValueError("judge identity belongs to a different judge implementation")
    return {
        **dict(value),
        "call_timeout_seconds": float(value["call_timeout_seconds"]),
        "review_timeout_seconds": float(value["review_timeout_seconds"]),
        "temperature": 0.0,
    }


def judge_identity_sha256(
    *,
    judge_model: str,
    api_base: str,
    call_timeout_seconds: float,
    review_timeout_seconds: float,
    max_attempts: int,
) -> str:
    """Hash the complete normalized judge identity payload."""

    return sha256_text(
        canonical_json(
            judge_identity_payload(
                judge_model=judge_model,
                api_base=api_base,
                call_timeout_seconds=call_timeout_seconds,
                review_timeout_seconds=review_timeout_seconds,
                max_attempts=max_attempts,
            )
        )
    )


def judged_inputs_sha256(
    golden_comments: Sequence[Mapping[str, Any]],
    candidates: Sequence[str],
    dedup_groups: Any,
    *,
    judge_identity: str,
) -> str:
    """Bind one evaluation to the exact inputs the judge compared.

    Completion keyed on (golden_url, tool) alone is not an identity: a
    same-configuration re-review exports different candidates under the same
    tool ID, and resuming against them would silently report the old export's
    metrics. Hashing the complete golden objects (not only comment text) and
    the versioned judge identity makes such records visibly stale.
    """

    return sha256_text(
        canonical_json(
            {
                "schema_version": JUDGED_INPUTS_VERSION,
                "judge_identity_sha256": judge_identity,
                "golden_comments": [dict(comment) for comment in golden_comments],
                "candidates": list(candidates),
                "dedup_groups": dedup_groups,
            }
        )
    )


def _validate_judge_result(value: Mapping[str, Any], *, require_exact_keys: bool) -> dict[str, Any]:
    """Return a typed semantic result or reject it without coercion."""

    expected = {"reasoning", "match", "confidence"}
    if require_exact_keys and set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise _JudgeResponseError("judge result has " + "; ".join(details))
    reasoning = value.get("reasoning")
    match = value.get("match")
    confidence = value.get("confidence")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise _JudgeResponseError("judge result reasoning must be non-empty text")
    if not isinstance(match, bool):
        raise _JudgeResponseError("judge result match must be a boolean")
    if not is_finite_number(confidence) or not 0 <= confidence <= 1:
        raise _JudgeResponseError("judge result confidence must be finite and within [0, 1]")
    return {
        "reasoning": reasoning.strip(),
        "match": match,
        "confidence": float(confidence),
    }


@asynccontextmanager
async def _evaluation_file_lock(path: Path) -> AsyncIterator[None]:
    """Serialize one evaluations file across local processes without blocking asyncio."""

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    locked = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        if locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@dataclass
class EvaluationState:
    """CodeReviewBench-compatible evaluation records with atomic persistence."""

    completed: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def is_done(
        self,
        golden_url: str,
        tool: str,
        inputs_sha256: str | None = None,
        judge_identity: str | None = None,
    ) -> bool:
        result = self.completed.get(golden_url, {}).get(tool)
        if not isinstance(result, Mapping) or result.get("errors_count", 0) != 0:
            return False
        if inputs_sha256 is None:
            return True
        # Records from before input binding carry no hash and are treated as
        # stale rather than silently trusted against unknown candidates.
        if result.get("judged_inputs_sha256") != inputs_sha256:
            return False
        return judge_identity is None or result.get("judge_identity_sha256") == judge_identity

    def mark_done(self, golden_url: str, tool: str, result: dict[str, Any]) -> None:
        self.completed.setdefault(golden_url, {})[tool] = result

    def clear_pair(self, golden_url: str, tool: str) -> bool:
        per_tool = self.completed.get(golden_url)
        if not isinstance(per_tool, dict) or tool not in per_tool:
            return False
        del per_tool[tool]
        if not per_tool:
            self.completed.pop(golden_url, None)
        return True

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
                try:
                    result = strict_json_loads(content)
                except json.JSONDecodeError:
                    raise
                except ValueError as exc:
                    raise _JudgeResponseError(f"judge result is not strict JSON: {exc}") from exc
                if not isinstance(result, dict):
                    raise ValueError("judge result must be a JSON object")
                validated = _validate_judge_result(result, require_exact_keys=True)
                validated["attempt_count"] = attempt + 1
                validated["retry_errors"] = retry_errors
                return validated
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
            except _JudgeResponseError as exc:
                retry_errors.append(self._safe_error(exc))
                if attempt == self.config.max_attempts - 1:
                    return {
                        "error": "Judge response failed semantic validation",
                        "attempt_count": attempt + 1,
                        "retry_errors": retry_errors,
                    }
                # A malformed completion is not a transport/rate-limit failure;
                # retry immediately rather than adding an unrelated backoff.
                continue
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
) -> dict[int, set[int]]:
    if not groups:
        return {}
    sibling_map: dict[int, set[int]] = {}
    for group in groups:
        group_indexes = {
            index
            for index in group
            if isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(candidates)
        }
        for index in group:
            if index in group_indexes:
                sibling_map[index] = group_indexes - {index}
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
        golden_index: {
            "comment": comment["comment"],
            "severity": comment.get("severity"),
            "category": comment.get("category"),
            "matched": False,
            "best_confidence": 0.0,
            "matched_candidate": None,
        }
        for golden_index, comment in enumerate(golden_comments)
    }
    candidate_matched = [False] * len(candidates)
    sibling_map = _build_sibling_map(candidates, dedup_groups)
    errors = []
    pair_matches: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        metadata = task_meta[index]
        golden = metadata["golden"]
        golden_index = metadata["golden_index"]
        candidate = metadata["candidate"]
        candidate_index = metadata["candidate_index"]
        if isinstance(result, BaseException) or not isinstance(result, Mapping):
            error = (
                f"{type(result).__name__}: {result}"
                if isinstance(result, BaseException)
                else f"invalid judge result type: {type(result).__name__}"
            )
            errors.append({"golden": golden, "candidate": candidate, "error": error})
            pair_matches.append(
                {
                    **metadata,
                    "match": False,
                    "confidence": 0.0,
                    "error": error,
                    "attempt_count": None,
                    "retry_errors": [],
                }
            )
            continue
        if result.get("error"):
            error = str(result["error"])
            errors.append({"golden": golden, "candidate": candidate, "error": error})
            pair_matches.append(
                {
                    **metadata,
                    "match": False,
                    "confidence": 0.0,
                    "reasoning": None,
                    "error": error,
                    "attempt_count": result.get("attempt_count", 1),
                    "retry_errors": result.get("retry_errors", []),
                }
            )
            continue
        try:
            semantic = _validate_judge_result(result, require_exact_keys=False)
        except _JudgeResponseError as exc:
            error = str(exc)
            errors.append({"golden": golden, "candidate": candidate, "error": error})
            pair_matches.append(
                {
                    **metadata,
                    "match": False,
                    "confidence": 0.0,
                    "reasoning": None,
                    "error": error,
                    "attempt_count": result.get("attempt_count", 1),
                    "retry_errors": result.get("retry_errors", []),
                }
            )
            continue
        pair_matches.append(
            {
                **metadata,
                **semantic,
                "error": None,
                "attempt_count": result.get("attempt_count", 1),
                "retry_errors": result.get("retry_errors", []),
            }
        )
        confidence = semantic["confidence"]
        if semantic["match"] and confidence > golden_matched[golden_index]["best_confidence"]:
            golden_matched[golden_index]["matched"] = True
            golden_matched[golden_index]["best_confidence"] = confidence
            golden_matched[golden_index]["matched_candidate"] = candidate
            golden_matched[golden_index]["reasoning"] = semantic["reasoning"]
            candidate_matched[candidate_index] = True
            for sibling in sibling_map.get(candidate_index, set()):
                candidate_matched[sibling] = True

    true_positives = []
    false_negatives = []
    for _golden_index, information in golden_matched.items():
        golden = information["comment"]
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
        {"candidate": candidates[index]}
        for index, matched in enumerate(candidate_matched)
        if not matched
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


def _verify_native_judge_bundle_locked(
    root: Path,
    model_dir: Path,
    *,
    judge_model: str,
    benchmark_data: Mapping[str, Any],
    candidates: Mapping[str, Any],
    dedup_groups: Mapping[str, Any],
) -> None:
    """Verify a native BugBunny bundle at the same locked snapshot we read."""

    index_path = model_dir / "bugbunny_export_index.json"
    committed_manifests = {path.resolve() for path in model_dir.glob("*_export_manifest.json")}
    if not index_path.is_file() and not committed_manifests:
        # Preserve compatibility with an untouched upstream CodeReviewBench
        # directory, which has no BugBunny metadata to verify. A BugBunny-shaped
        # row with no metadata is instead the signature of an interrupted first
        # export and must never be judged as committed output.
        phantom_tools: set[str] = set()
        for per_case in (*candidates.values(), *dedup_groups.values()):
            if isinstance(per_case, Mapping):
                phantom_tools.update(
                    str(tool) for tool in per_case if _BUGBUNNY_TOOL_ID.fullmatch(str(tool))
                )
        for entry in benchmark_data.values():
            if isinstance(entry, Mapping):
                phantom_tools.update(
                    str(review.get("tool"))
                    for review in entry.get("reviews", [])
                    if isinstance(review, Mapping)
                    and _BUGBUNNY_TOOL_ID.fullmatch(str(review.get("tool") or ""))
                )
        if phantom_tools:
            raise JudgeError(
                "BugBunny Step 3 rows exist without a committed manifest/index: "
                + ", ".join(sorted(phantom_tools))
            )
        return
    verified_manifests: dict[Path, Mapping[str, Any]] = {}
    common_output_hashes: dict[str, Any] | None = None
    for manifest_path in sorted(committed_manifests):
        try:
            verified = verify_codereviewbench_export_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            raise JudgeError(f"BugBunny export verification failed: {exc}") from exc
        hashes = dict(verified.get("output_files_sha256") or {})
        if common_output_hashes is None:
            common_output_hashes = hashes
        elif hashes != common_output_hashes:
            raise JudgeError("BugBunny export manifests identify different Step 3 inputs")
        verified_manifests[manifest_path] = verified
    if not index_path.is_file():
        # The low-level exporter intentionally returns a manifest without the
        # CLI's cumulative index; all manifests above still bind one verified
        # snapshot, so it is safe to judge.
        return
    index = _load_object(index_path, required=True)
    if (
        index.get("schema_version") != EXPORT_INDEX_SCHEMA_VERSION
        or index.get("implementation") != implementation_identity()
        or index.get("judge_model") != judge_model
    ):
        raise JudgeError("BugBunny export index has an unsupported identity")
    output_hashes = index.get("output_files_sha256")
    output_paths = {
        "benchmark_data.json": root / "benchmark_data.json",
        f"{model_dir.name}/candidates.json": model_dir / "candidates.json",
        f"{model_dir.name}/dedup_groups.json": model_dir / "dedup_groups.json",
    }
    if not isinstance(output_hashes, Mapping) or set(output_hashes) != set(output_paths):
        raise JudgeError("BugBunny export index does not bind every Step 3 input")
    if any(
        not path.is_file() or sha256_bytes(path.read_bytes()) != output_hashes[relative]
        for relative, path in output_paths.items()
    ):
        raise JudgeError("BugBunny Step 3 inputs do not match the export index")
    exports = index.get("exports")
    if not isinstance(exports, list) or not exports:
        raise JudgeError("BugBunny export index contains no tracks")
    indexed_manifests: set[Path] = set()
    for raw_export in exports:
        if not isinstance(raw_export, Mapping):
            raise JudgeError("BugBunny export index contains a malformed track")
        relative = raw_export.get("manifest")
        expected_sha256 = raw_export.get("manifest_sha256")
        if not isinstance(relative, str) or not relative or not isinstance(expected_sha256, str):
            raise JudgeError("BugBunny export index has an incomplete manifest binding")
        manifest_path = (root / relative).resolve()
        if (
            manifest_path.parent != model_dir
            or manifest_path in indexed_manifests
            or not manifest_path.is_file()
            or sha256_bytes(manifest_path.read_bytes()) != expected_sha256
        ):
            raise JudgeError("BugBunny export index references a missing or changed manifest")
        indexed_manifests.add(manifest_path)
        verified = verified_manifests.get(manifest_path)
        if verified is None:
            raise JudgeError("BugBunny export index references an uncommitted manifest")
        if verified.get("tool_id") != raw_export.get("tool_id") or verified.get(
            "output_files_sha256"
        ) != dict(output_hashes):
            raise JudgeError("BugBunny export index metadata differs from its manifest")
    if indexed_manifests != committed_manifests:
        raise JudgeError("BugBunny export index does not enumerate every committed manifest")


def _load_judge_input_snapshot(
    root: Path,
    model_dir: Path,
    *,
    judge_model: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read one transactionally consistent Step 3 snapshot."""

    with file_lock(root / ".bugbunny-export.lock"):
        benchmark_data = _load_object(root / "benchmark_data.json", required=True)
        candidates = _load_object(model_dir / "candidates.json", required=False)
        dedup_groups = _load_object(model_dir / "dedup_groups.json", required=False)
        _verify_native_judge_bundle_locked(
            root,
            model_dir,
            judge_model=judge_model,
            benchmark_data=benchmark_data,
            candidates=candidates,
            dedup_groups=dedup_groups,
        )
        return benchmark_data, candidates, dedup_groups


def aggregate_metrics(
    state: EvaluationState,
    *,
    tools: set[str] | None = None,
    population: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Reduce evaluation rows to per-tool totals.

    ``population`` restricts aggregation to the exact case/tool matrix of the
    current invocation. A tool-only filter is insufficient when a benchmark
    selection removes a case while retaining the same tool.
    """

    totals: dict[str, dict[str, Any]] = {}
    for golden_url, per_tool in state.completed.items():
        for tool, result in per_tool.items():
            if population is not None and (golden_url, tool) not in population:
                continue
            if tools is not None and tool not in tools:
                continue
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
    """Evaluate selected review/tool pairs under one cross-process state lease.

    The lease covers load, resume selection, judging, and every checkpoint. A
    second local process therefore reloads the first process's committed rows
    instead of overwriting them from a stale in-memory snapshot.
    """

    if not isinstance(judge_model, str) or not judge_model.strip():
        raise ValueError("judge model must not be empty")
    if not isinstance(api_base, str) or not api_base.strip():
        raise ValueError("judge API base must not be empty")
    _require_positive_int(judge_concurrency, label="judge concurrency")
    _require_positive_int(review_concurrency, label="review concurrency")
    _require_positive_finite(call_timeout_seconds, label="judge call timeout")
    _require_positive_finite(review_timeout_seconds, label="judge review timeout")
    _require_positive_int(max_attempts, label="judge max attempts")

    root = results_dir.expanduser().resolve()
    model_dir = root / sanitize_model_name(judge_model)
    output_path = (
        evaluations_file.expanduser().resolve()
        if evaluations_file is not None
        else model_dir / "evaluations.json"
    )
    lock_path = output_path.with_name(f".{output_path.name}.lock")
    async with _evaluation_file_lock(lock_path):
        return await _run_codereviewbench_judge_locked(
            results_dir=results_dir,
            judge_model=judge_model,
            api_key=api_key,
            api_base=api_base,
            tools=tools,
            judge_concurrency=judge_concurrency,
            review_concurrency=review_concurrency,
            call_timeout_seconds=call_timeout_seconds,
            review_timeout_seconds=review_timeout_seconds,
            max_attempts=max_attempts,
            force=force,
            evaluations_file=evaluations_file,
            judge=judge,
        )


async def _run_codereviewbench_judge_locked(
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
    """Locked implementation for :func:`run_codereviewbench_judge`."""

    root = results_dir.expanduser().resolve()
    model_dir = root / sanitize_model_name(judge_model)
    benchmark_data, all_candidates, all_dedup_groups = await asyncio.to_thread(
        _load_judge_input_snapshot,
        root,
        model_dir,
        judge_model=judge_model,
    )
    output_path = (
        evaluations_file.expanduser().resolve()
        if evaluations_file is not None
        else model_dir / "evaluations.json"
    )
    selected_tools = set(tools) if tools is not None else None
    if selected_tools is not None and (not selected_tools or "" in selected_tools):
        raise JudgeError("--tool values must be non-empty")
    state = EvaluationState.load(output_path)
    identity_payload = judge_identity_payload(
        judge_model=judge_model,
        api_base=api_base,
        call_timeout_seconds=call_timeout_seconds,
        review_timeout_seconds=review_timeout_seconds,
        max_attempts=max_attempts,
    )
    identity_sha256 = sha256_text(canonical_json(identity_payload))

    work_items: list[
        tuple[str, list[dict[str, Any]], dict[str, Any], str, list[str], Any, str]
    ] = []
    scope_tools: set[str] = set()
    scope_population: set[tuple[str, str]] = set()
    available_tools: set[str] = set()
    stale_rows_removed = False
    skipped = 0
    for golden_url, raw_entry in benchmark_data.items():
        if not isinstance(golden_url, str) or not isinstance(raw_entry, Mapping):
            raise JudgeError("benchmark_data.json contains an invalid case")
        entry = dict(raw_entry)
        raw_golden_comments = entry.get("golden_comments", [])
        reviews = entry.get("reviews", [])
        if not isinstance(raw_golden_comments, list) or not isinstance(reviews, list):
            raise JudgeError(f"benchmark case has invalid reviews/comments: {golden_url}")
        if any(
            not isinstance(comment, Mapping) or not isinstance(comment.get("comment"), str)
            for comment in raw_golden_comments
        ):
            raise JudgeError(f"benchmark case has invalid golden comments: {golden_url}")
        golden_comments = [dict(comment) for comment in raw_golden_comments]
        for raw_review in reviews:
            if not isinstance(raw_review, Mapping) or not isinstance(raw_review.get("tool"), str):
                raise JudgeError(f"benchmark case has an invalid review: {golden_url}")
            review = dict(raw_review)
            tool = review["tool"]
            available_tools.add(tool)
            if selected_tools is not None and tool not in selected_tools:
                continue
            if (golden_url, tool) in scope_population:
                raise JudgeError(f"benchmark duplicates review for {golden_url} / {tool}")
            scope_tools.add(tool)
            scope_population.add((golden_url, tool))
            candidates = get_candidates(review, all_candidates, golden_url)
            per_url_groups = all_dedup_groups.get(golden_url)
            groups = per_url_groups.get(tool) if isinstance(per_url_groups, Mapping) else None
            if groups is not None and not isinstance(groups, list):
                raise JudgeError(f"dedup groups are invalid for {golden_url} / {tool}")
            inputs_sha256 = judged_inputs_sha256(
                golden_comments,
                candidates,
                groups,
                judge_identity=identity_sha256,
            )
            if not force and state.is_done(golden_url, tool, inputs_sha256, identity_sha256):
                skipped += 1
                continue
            if not force:
                stale_rows_removed = state.clear_pair(golden_url, tool) or stale_rows_removed
            work_items.append(
                (golden_url, golden_comments, review, tool, candidates, groups, inputs_sha256)
            )

    if selected_tools is not None:
        missing_tools = selected_tools - available_tools
        if missing_tools:
            raise JudgeError(
                "requested --tool values are absent from benchmark data: "
                + ", ".join(sorted(missing_tools))
            )
    if not force:
        # A same-tool re-export may intentionally shrink its case population.
        # Rows for removed cases are just as stale as rows whose candidate text
        # changed: hiding them from this summary is insufficient because bound
        # analysis correctly rejects extra evaluation rows. Persist their
        # removal before any replacement/model call can time out or crash.
        for stale_url, per_tool in list(state.completed.items()):
            if not isinstance(per_tool, Mapping):
                continue
            for stale_tool in tuple(per_tool):
                if stale_tool in scope_tools and (stale_url, stale_tool) not in scope_population:
                    stale_rows_removed = (
                        state.clear_pair(stale_url, stale_tool) or stale_rows_removed
                    )
    if force:
        state.clear_tools(selected_tools)
        state.save(output_path)
    elif stale_rows_removed:
        # Commit invalidation before making any model calls. If the replacement
        # times out or the process crashes, stale metrics cannot reappear.
        await asyncio.to_thread(state.save, output_path)

    # Start expensive reviews first. This changes only queue order; each
    # review's comparison order and metric reduction remain unchanged.
    work_items.sort(key=lambda item: (-(len(item[1]) * len(item[4])), item[0], item[3]))
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
    save_running = False
    save_pending = False

    async def persist_state() -> None:
        """Coalesce full-state checkpoint writes.

        Every save persists the complete state, so concurrent completions only
        need the latest snapshot to reach disk — one writer drains a pending
        flag instead of every completion serializing an O(N) rewrite. Rows
        completed after the final snapshot of a crashed run are simply
        re-judged on resume, exactly as before.
        """

        nonlocal save_running, save_pending
        if save_running:
            save_pending = True
            return
        save_running = True
        try:
            while True:
                save_pending = False
                async with state_lock:
                    snapshot = (
                        json.dumps(state.completed, indent=2, sort_keys=True, ensure_ascii=False)
                        + "\n"
                    )
                await asyncio.to_thread(atomic_write_text, output_path, snapshot)
                if not save_pending:
                    return
        finally:
            save_running = False

    async def evaluate_item(
        item: tuple[str, list[dict[str, Any]], dict[str, Any], str, list[str], Any, str],
    ) -> None:
        nonlocal evaluated, timed_out
        golden_url, golden_comments, review, tool, candidates, groups, inputs_sha256 = item
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
        result["judged_inputs_sha256"] = inputs_sha256
        result["judge_identity_version"] = JUDGE_IDENTITY_VERSION
        result["judge_identity"] = identity_payload
        result["judge_identity_sha256"] = identity_sha256
        async with state_lock:
            state.mark_done(golden_url, tool, result)
            evaluated += 1
        await persist_state()

    try:
        await asyncio.gather(*(evaluate_item(item) for item in work_items))
        await persist_state()
    finally:
        if owns_judge:
            await judge.aclose()

    metrics = aggregate_metrics(state, tools=scope_tools, population=scope_population)
    return {
        "evaluations_file": str(output_path),
        "judge_model": judge_model,
        "judge_identity_version": JUDGE_IDENTITY_VERSION,
        "judge_identity": identity_payload,
        "judge_identity_sha256": identity_sha256,
        "evaluated": evaluated,
        "resumed": skipped,
        "timed_out": timed_out,
        "case_tool_population": len(scope_population),
        "metrics": metrics,
    }


__all__ = [
    "JUDGED_INPUTS_VERSION",
    "JUDGE_IDENTITY_VERSION",
    "JUDGE_PROMPT",
    "EvaluationState",
    "JudgeConfig",
    "JudgeError",
    "MartianJudge",
    "aggregate_metrics",
    "evaluate_review",
    "get_candidates",
    "judge_identity_payload",
    "judge_identity_sha256",
    "judged_inputs_sha256",
    "run_codereviewbench_judge",
    "validate_judge_identity_payload",
]
