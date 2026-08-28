from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any

from bugbunny import __version__
from bugbunny.build import REVIEW_SCHEMA_VERSION, implementation_identity
from bugbunny.context import ContextBuilder, display_path
from bugbunny.diff import DiffChunk, ParsedDiff, parse_unified_diff
from bugbunny.exploration import (
    EXPLORATION_PROMPT_VERSION,
    EXPLORATION_SCHEMA_VERSION,
    SharedBlobBudget,
    exploration_prompt_sha256,
    exploration_schema_sha256,
    explore_repository_context,
)
from bugbunny.families import consolidate_semantic_duplicates
from bugbunny.gateway import GatewayError, ModelGateway
from bugbunny.models import (
    DECLARED_WINDOW_CHARS_PER_TOKEN,
    DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS,
    CallRecord,
    Coverage,
    Finding,
    PRInfo,
    RejectedFinding,
    ReviewArtifact,
    ReviewConfig,
)
from bugbunny.prompts import (
    GENERATION_PROMPT_VERSION,
    VERIFIER_PROMPT_VERSION,
    build_generation_prompt,
    build_verifier_prompt,
    generation_metadata_provenance,
    generation_prompt_sha256,
    verifier_candidate_payload,
    verifier_prompt_sha256,
)
from bugbunny.report import render_markdown
from bugbunny.repository import GitRepositoryCache, RepositorySnapshot
from bugbunny.schemas import (
    GENERATION_TRANSPORT_SCHEMA,
    VERIFIER_MAX_BATCH,
    VERIFIER_SCHEMA,
    PayloadValidationError,
    findings_from_payload_tolerant,
    validate_verifier_payload,
)
from bugbunny.util import (
    acquire_semaphore_bounded,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    git_lines,
    monotonic_ms,
    sha256_text,
    utc_now,
)
from bugbunny.validation import (
    apply_verifier_decisions,
    changed_line_ranges,
    validate_findings,
)


class ReviewEngineError(RuntimeError):
    """The review could not produce a scoreable, fully covered artifact."""


@dataclass(frozen=True)
class _GenerationBatch:
    batch_id: str
    chunks: tuple[DiffChunk, ...]
    patch: str
    context: str


@dataclass(frozen=True)
class _SemaphoreGateway:
    """Apply the review-local call limit without serializing local retrieval."""

    gateway: Any
    semaphore: asyncio.Semaphore

    async def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        queue_timeout = kwargs.get("queue_timeout_seconds")
        if queue_timeout is not None and (
            not isinstance(queue_timeout, (int, float))
            or isinstance(queue_timeout, bool)
            or not math.isfinite(queue_timeout)
            or queue_timeout <= 0
        ):
            raise ValueError("queue_timeout_seconds must be finite and positive")
        normalized_queue_timeout = float(queue_timeout) if queue_timeout is not None else None
        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        try:
            if normalized_queue_timeout is None:
                await self.semaphore.acquire()
            else:
                await acquire_semaphore_bounded(self.semaphore, normalized_queue_timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"review-local model request queue wait exceeded {normalized_queue_timeout:g}s"
            ) from exc
        try:
            if normalized_queue_timeout is not None:
                remaining = normalized_queue_timeout - (loop.time() - queued_at)
                if remaining <= 0:
                    raise TimeoutError(
                        "review-local model request queue wait exceeded "
                        f"{normalized_queue_timeout:g}s"
                    )
                # The local and gateway-wide queues share one end-to-end queue
                # deadline; each layer receives only the remaining budget.
                kwargs = {**kwargs, "queue_timeout_seconds": remaining}
            return await self.gateway.complete_json(prompt, **kwargs)
        finally:
            self.semaphore.release()


def _operation_deadline_seconds(transport_timeout_seconds: float) -> float:
    """Whole-call execution bound for generation and verification calls.

    The transport read timeout is per-socket-read, so a trickle-body response
    (one byte per interval) evades it indefinitely and holds a review plus a
    global-LLM slot forever. The whole-call bound leaves linear headroom for
    the gateway's bounded retry ladder and backoff sleeps, so a legitimate
    retried call is never cut short while a hung one always ends. Queue wait
    is deliberately not covered: with executions bounded, queue drain is
    already finite, and a queue deadline here would turn a busy sweep into
    spurious coverage failures.
    """

    return float(transport_timeout_seconds) * 4


def _generation_batches(
    chunks: Sequence[DiffChunk],
    contexts: Mapping[str, str],
    *,
    max_patch_chars: int,
    max_context_chars: int,
) -> tuple[_GenerationBatch, ...]:
    """Pack small file chunks into the fewest bounded model calls.

    Diff chunking remains file-local so its lossless accounting is simple. This
    second deterministic layer removes the latency penalty of making one model
    call per small file while retaining the original chunk IDs for coverage.
    """

    groups: list[list[DiffChunk]] = []
    pending: list[DiffChunk] = []
    pending_chars = 0
    separator = "\n\n"
    for chunk in chunks:
        extra = len(chunk.annotated_patch) + (len(separator) if pending else 0)
        if pending and pending_chars + extra > max_patch_chars:
            groups.append(pending)
            pending = []
            pending_chars = 0
            extra = len(chunk.annotated_patch)
        pending.append(chunk)
        pending_chars += extra
    if pending:
        groups.append(pending)

    batches: list[_GenerationBatch] = []
    for index, group in enumerate(groups):
        patch = separator.join(chunk.annotated_patch for chunk in group)
        headers = [
            f"### CONTEXT {chunk.chunk_id} ({display_path(chunk.path)})\n" for chunk in group
        ]
        # Headers and separators are part of the rendered context, so they
        # come out of the budget before it is divided among chunk bodies;
        # otherwise the final hard slice always blanks the trailing chunks'
        # seed context in multi-chunk batches.
        overhead = sum(len(header) for header in headers) + len(separator) * (len(group) - 1)
        body_budget = max(0, max_context_chars - overhead)
        per_chunk = max(1, body_budget // len(group)) if body_budget > 0 else 0
        context = separator.join(
            f"{header}{contexts.get(chunk.chunk_id, '')[:per_chunk]}"
            for header, chunk in zip(headers, group, strict=True)
        )[:max_context_chars]
        member_ids = [chunk.chunk_id for chunk in group]
        batch_id = (
            member_ids[0]
            if len(member_ids) == 1
            else f"b{index:04d}-{sha256_text(canonical_json(member_ids))[:12]}"
        )
        batches.append(_GenerationBatch(batch_id, tuple(group), patch, context))
    return tuple(batches)


def _assign_source_chunk(
    finding: Finding,
    batch: _GenerationBatch,
    *,
    left_path_aliases: Mapping[str, str] | None = None,
) -> bool:
    review_path = (
        (left_path_aliases or {}).get(finding.path, finding.path)
        if finding.side == "LEFT"
        else finding.path
    )
    matches = [
        chunk
        for chunk in batch.chunks
        if chunk.path == review_path
        and finding.line in (chunk.added_lines if finding.side == "RIGHT" else chunk.deleted_lines)
    ]
    if len(matches) == 1:
        # GitHub and native artifacts use the review-side path. For renamed
        # files a model may naturally copy the old path from the `---` header;
        # normalize it only after proving the old/new pair belongs to this
        # exact chunk.
        finding.path = matches[0].path
        finding.chunk_id = matches[0].chunk_id
        return True
    return False


def _runtime_record(
    gateway: ModelGateway,
    model: str,
    *,
    reasoning_effort: str,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    runtime_method = getattr(gateway, "runtime_provenance", None)
    if callable(runtime_method):
        try:
            result = dict(runtime_method(model))
            limits = result.get("limits")
            if max_output_tokens is not None and isinstance(limits, Mapping):
                resolved_limits = dict(limits)
                resolved_limits["max_output_tokens"] = max_output_tokens
                resolved_limits["max_output_tokens_per_call_override"] = True
                result["limits"] = resolved_limits
            supported = (
                limits.get("reasoning_effort_parameter_will_be_sent")
                if isinstance(limits, Mapping)
                else None
            )
            result["requested_reasoning_effort"] = reasoning_effort
            result["reasoning_effort_parameter_will_be_sent"] = (
                bool(supported) if supported is not None else None
            )
            return result
        except (OSError, ValueError, RuntimeError, TypeError):
            return {
                "requested_model": model,
                "requested_reasoning_effort": reasoning_effort,
                "reasoning_effort_parameter_will_be_sent": None,
                "unavailable": True,
            }
    return {
        "requested_model": model,
        "requested_reasoning_effort": reasoning_effort,
        "reasoning_effort_parameter_will_be_sent": None,
        "transport": type(gateway).__name__,
    }


def review_runtime_provenance(
    gateway: ModelGateway,
    *,
    model: str,
    verifier_model: str | None,
    generation_reasoning_effort: str,
    verifier_reasoning_effort: str,
    generation_max_output_tokens: int | None = None,
    verifier_max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Return both transport identities that can influence final findings."""

    effective_verifier = model if verifier_model == "same" else verifier_model
    return {
        "generation": _runtime_record(
            gateway,
            model,
            reasoning_effort=generation_reasoning_effort,
            max_output_tokens=generation_max_output_tokens,
        ),
        "verification": (
            _runtime_record(
                gateway,
                effective_verifier,
                reasoning_effort=verifier_reasoning_effort,
                max_output_tokens=verifier_max_output_tokens,
            )
            if effective_verifier not in {None, "none"}
            else None
        ),
    }


def _verification_batches(
    findings: Sequence[Finding],
    *,
    max_items: int,
    max_chars: int,
    max_prompt_chars: int | None = None,
) -> tuple[tuple[int, list[Finding]], ...]:
    """Partition candidates without dropping any or starving verifier evidence."""

    batches: list[tuple[int, list[Finding]]] = []
    current: list[Finding] = []
    offset = 0
    for finding in findings:
        trial = [*current, finding]
        candidate_payload_too_large = len(verifier_candidate_payload(trial)) > max_chars
        prompt_too_large = (
            len(trial) <= max_items
            and max_prompt_chars is not None
            and len(build_verifier_prompt(trial, "", "", max_batch_size=max_items))
            > max_prompt_chars - 4_096
        )
        if current and (
            len(current) >= max_items or candidate_payload_too_large or prompt_too_large
        ):
            batches.append((offset, current))
            offset += len(current)
            current = []
            trial = [finding]
        if len(verifier_candidate_payload(trial)) > max_chars:
            raise ReviewEngineError("one verifier candidate exceeds verification_batch_chars")
        if (
            max_prompt_chars is not None
            and len(build_verifier_prompt(trial, "", "", max_batch_size=max_items))
            > max_prompt_chars - 4_096
        ):
            raise ReviewEngineError(
                "one verifier candidate leaves fewer than 4096 planned characters for evidence"
            )
        current.append(finding)
    if current:
        batches.append((offset, current))
    return tuple(batches)


def _fit_generation_prompt(
    batch: _GenerationBatch,
    *,
    max_input_chars: int | None,
    pr_title: str,
    pr_body: str,
    allowed_categories: Sequence[str],
    review_policy: str,
) -> tuple[str, _GenerationBatch, bool]:
    """Fit exact rendered generation input, clipping context before patch bytes."""

    def render(context: str) -> str:
        return build_generation_prompt(
            batch.patch,
            context,
            pr_title=pr_title,
            pr_body=pr_body,
            chunk_id=batch.batch_id,
            allowed_categories=allowed_categories,
            review_policy=review_policy,
        )

    prompt = render(batch.context)
    if max_input_chars is None or len(prompt) <= max_input_chars:
        return prompt, batch, False
    if len(render("")) > max_input_chars:
        raise ReviewEngineError(
            f"generation patch and framing exceed the {max_input_chars}-character input plan"
        )

    marker = (
        "\n[BUGBUNNY_TRUNCATED_GENERATION_CONTEXT "
        f"original_chars={len(batch.context)} sha256={sha256_text(batch.context)}]"
    )
    low = 0
    high = len(batch.context)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = batch.context[:middle] + marker
        if len(render(candidate)) <= max_input_chars:
            low = middle
        else:
            high = middle - 1
    clipped_context = batch.context[:low] + marker
    prompt = render(clipped_context)
    if len(prompt) > max_input_chars:
        # Reachable only when the headroom is smaller than the truncation
        # marker itself. The earlier render("") check proved an empty context
        # fits, so degrade to that instead of failing a fittable batch; the
        # omission is still recorded through the clipped flag.
        clipped_context = ""
        prompt = render(clipped_context)
    return prompt, replace(batch, context=clipped_context), True


def _fit_verifier_prompt(
    findings: Sequence[Finding],
    patch: str,
    context: str,
    *,
    max_batch_size: int,
    max_input_chars: int | None,
    retry_notice: str = "",
) -> tuple[str, str, bool]:
    """Fit exact verifier input while retaining its anchor-preserving patch."""

    def render(value: str) -> str:
        return (
            build_verifier_prompt(
                findings,
                patch,
                value,
                max_batch_size=max_batch_size,
            )
            + retry_notice
        )

    prompt = render(context)
    if max_input_chars is None or len(prompt) <= max_input_chars:
        return prompt, context, False
    if len(render("")) > max_input_chars:
        raise ReviewEngineError(
            f"verifier candidates and patch exceed the {max_input_chars}-character input plan"
        )
    low = 0
    high = len(context)
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(context[:middle])) <= max_input_chars:
            low = middle
        else:
            high = middle - 1
    clipped = context[:low]
    prompt = render(clipped)
    if len(prompt) > max_input_chars:
        raise ReviewEngineError("verifier prompt fitting failed to honor its character plan")
    return prompt, clipped, True


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:2_000]


def _context_prompt(bundle: Any, chunk_id: str) -> str:
    by_chunk = getattr(bundle, "by_chunk", None)
    if not isinstance(by_chunk, Mapping):
        raise ReviewEngineError("context builder returned no by_chunk mapping")
    packet = by_chunk.get(chunk_id)
    if packet is None:
        raise ReviewEngineError(f"context builder omitted chunk {chunk_id}")
    if isinstance(packet, str):
        return packet
    prompt = getattr(packet, "prompt", None)
    if not isinstance(prompt, str):
        raise ReviewEngineError(f"context packet {chunk_id} has no prompt text")
    return prompt


def _numbered_source_row(value: str) -> bool:
    prefix, separator, _text = value.partition("|")
    return bool(separator and prefix.strip().isdigit())


def _source_block_exposes_path(lines: Sequence[str], header_index: int) -> bool:
    """Require an actual numbered source row after a structured file header."""

    for line in lines[header_index + 1 :]:
        if line.startswith(("### ", "CONTEXT FOR DIFF CHUNK", "UNTRUSTED ")):
            return False
        if _numbered_source_row(line):
            return True
    return False


def _context_exposes_path(context: str, path: str) -> bool:
    """Recognize only BugBunny's structured file evidence, never substrings.

    Splitting is LF-only and paths are matched in their escaped display form:
    ``str.splitlines()`` breaks on \\f/\\v/\\x1c-\\x1e/\\x85/U+2028/29 inside
    source rows, and a raw control-character path would fragment its own
    marker line — both mis-reporting a genuinely exposed file as omitted.
    """

    lines = context.split("\n")
    shown = display_path(path)
    read_header = f"UNTRUSTED IMMUTABLE HEAD FILE {shown} L"
    source_header_suffixes = (
        f"### RIGHT source: {shown}:",
        f"### LEFT source: {shown}:",
    )
    search_prefix = f"{shown}:"
    evidence_marker = f" — {shown}:"
    for index, line in enumerate(lines):
        if line.startswith(read_header) and _source_block_exposes_path(lines, index):
            return True
        if line.startswith(source_header_suffixes) and _source_block_exposes_path(lines, index):
            return True
        if line.startswith(search_prefix):
            line_number = line[len(search_prefix) :].split(":", 1)[0]
            if line_number.isdigit():
                return True
        if evidence_marker in line:
            line_number = line.split(evidence_marker, 1)[1].split(":", 1)[0]
            if line_number.isdigit():
                return True

    path_header = f"Path: {shown}"
    for index, line in enumerate(lines):
        if line != path_header:
            continue
        for source_index in range(index + 1, len(lines)):
            candidate = lines[source_index]
            if candidate.startswith(("### CONTEXT ", "CONTEXT FOR DIFF CHUNK")):
                break
            if candidate == "SURROUNDING SOURCE:":
                if _source_block_exposes_path(lines, source_index):
                    return True
                break
    return False


def _verifier_source_exposes_path(context: str, path: str) -> bool:
    lines = context.split("\n")
    shown = display_path(path)
    headers = (f"### RIGHT source: {shown}:", f"### LEFT source: {shown}:")
    return any(
        line.startswith(headers) and _source_block_exposes_path(lines, index)
        for index, line in enumerate(lines)
    )


def _verifier_generation_context(context: str) -> str:
    lines = context.split("\n")
    first_source = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(("### RIGHT source: ", "### LEFT source: "))
        ),
        len(lines),
    )
    return "\n".join(lines[:first_source])


def _context_summary(bundle: Any, *, review_policy: str = "production") -> dict[str, Any]:
    summary: dict[str, Any] = {
        "generation_prompt_version": GENERATION_PROMPT_VERSION,
        "generation_prompt_sha256": generation_prompt_sha256(review_policy),
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "verifier_prompt_sha256": verifier_prompt_sha256(),
        "context_selection_prompt_version": EXPLORATION_PROMPT_VERSION,
        "context_selection_prompt_sha256": exploration_prompt_sha256(),
        "context_selection_schema_version": EXPLORATION_SCHEMA_VERSION,
    }
    for name in ("stats", "statistics"):
        value = getattr(bundle, name, None)
        if value is None:
            continue
        if isinstance(value, Mapping):
            summary[name] = dict(value)
        elif is_dataclass(value):
            summary[name] = asdict(value)
        elif hasattr(value, "to_dict"):
            summary[name] = value.to_dict()
    by_chunk = getattr(bundle, "by_chunk", {})
    if isinstance(by_chunk, Mapping):
        summary["packet_count"] = len(by_chunk)
        summary["prompt_chars"] = sum(
            len(packet if isinstance(packet, str) else getattr(packet, "prompt", ""))
            for packet in by_chunk.values()
        )
        summary["packets"] = {
            str(chunk_id): {
                "prompt_sha256": sha256_text(
                    packet if isinstance(packet, str) else getattr(packet, "prompt", "")
                ),
                "prompt_chars": len(
                    packet if isinstance(packet, str) else getattr(packet, "prompt", "")
                ),
                "symbols": list(getattr(packet, "symbols", ())),
                "definitions": len(getattr(packet, "definitions", ())),
                "usages": len(getattr(packet, "usages", ())),
                "imports": len(getattr(packet, "imports", ())),
                "callers": len(getattr(packet, "callers", ())),
                "tests": len(getattr(packet, "tests", ())),
                "hypotheses": len(getattr(packet, "hypotheses", ())),
                "truncated": bool(getattr(packet, "truncated", False)),
                "diagnostics": list(getattr(packet, "diagnostics", ())),
            }
            for chunk_id, packet in by_chunk.items()
        }
    for name in ("exclusions", "diagnostics"):
        value = getattr(bundle, name, None)
        if value:
            summary[name] = list(value)
    return summary


def _call_token_summary(calls: Sequence[CallRecord]) -> dict[str, Any]:
    """Aggregate provider-reported prompt usage without presenting estimates as exact."""

    result: dict[str, Any] = {}
    for stage in dict.fromkeys(call.stage for call in calls):
        stage_calls = [call for call in calls if call.stage == stage]
        reported = [call.input_tokens for call in stage_calls if call.input_tokens is not None]
        result[stage] = {
            "calls": len(stage_calls),
            "calls_with_provider_reported_input_tokens": len(reported),
            "provider_reported_input_tokens": sum(reported),
            "calls_missing_provider_reported_input_tokens": len(stage_calls) - len(reported),
        }
    return result


def _anchor_centered_rows(rows: Sequence[str], anchor_index: int, *, max_chars: int | None) -> str:
    if not rows or (max_chars is not None and max_chars <= 0):
        return ""
    if max_chars is None:
        return "\n".join(rows)
    selected = {anchor_index}
    for distance in range(1, len(rows)):
        for candidate in (anchor_index - distance, anchor_index + distance):
            if not 0 <= candidate < len(rows):
                continue
            proposed = "\n".join(rows[row] for row in sorted((*selected, candidate)))
            if len(proposed) <= max_chars:
                selected.add(candidate)
    rendered = "\n".join(rows[row] for row in sorted(selected))
    return rendered[:max_chars]


def _line_source(
    snapshot: RepositorySnapshot,
    finding: Finding,
    *,
    base_sha: str,
    base_path: str | None = None,
    radius: int = 18,
    max_chars: int | None = None,
) -> str:
    source = (
        snapshot.read_text(finding.path)
        if finding.side == "RIGHT"
        else snapshot.read_blob(base_sha, base_path or finding.path)
    )
    rows = git_lines(source)
    if not rows:
        return ""
    first = max(1, finding.line - radius)
    last = min(len(rows), finding.line + radius)
    excerpt_rows = [f"{number:>6} | {rows[number - 1]}" for number in range(first, last + 1)]
    # Grow outwards from the finding instead of clipping the beginning of the
    # source window. This keeps the changed-line anchor visible when a long
    # source excerpt receives only a small fair share of verifier context.
    anchor_index = finding.line - first
    return _anchor_centered_rows(excerpt_rows, anchor_index, max_chars=max_chars)


def _fair_block_allocations(maximums: Sequence[int], budget: int) -> tuple[int, ...]:
    """Water-fill a rendered-text budget across blocks without first-item bias."""

    if not maximums:
        return ()
    content_budget = max(0, budget - 2 * (len(maximums) - 1))
    allocations = [0] * len(maximums)
    remaining = content_budget
    active = list(range(len(maximums)))
    while active and remaining > 0:
        share, remainder = divmod(remaining, len(active))
        satisfied = [index for index in active if maximums[index] <= share]
        if satisfied:
            for index in satisfied:
                allocations[index] = maximums[index]
                remaining -= maximums[index]
            active = [index for index in active if index not in satisfied]
            continue
        for position, index in enumerate(active):
            allocations[index] = share + (1 if position < remainder else 0)
        remaining = 0
    return tuple(allocations)


def _render_fair_blocks(blocks: Sequence[str], budget: int) -> str:
    allocations = _fair_block_allocations([len(block) for block in blocks], budget)
    return "\n\n".join(
        block[:allocation]
        for block, allocation in zip(blocks, allocations, strict=True)
        if allocation > 0
    )


def _coordinate_matches(prefix: str, line: int, side: str) -> bool:
    """Match the exact annotated gutter coordinate for one changed line.

    Substring matching would let ``R2`` anchor to an ``R2D2.py`` file header or
    an ``R21`` row and center the verifier's evidence on the wrong code.
    """

    coordinate = prefix.strip()
    if side == "RIGHT":
        return coordinate == f"R{line}" or coordinate.startswith(f"R{line}/")
    return coordinate == f"L{line}" or coordinate.endswith(f"/L{line}")


def _anchor_patch_excerpt(
    chunk: DiffChunk,
    line: int,
    side: str = "RIGHT",
    radius: int = 14,
    max_chars: int | None = None,
) -> str:
    rows = git_lines(chunk.annotated_patch)
    matches = []
    for index, row in enumerate(rows):
        prefix, separator, _rest = row.partition(" | ")
        if separator and _coordinate_matches(prefix, line, side):
            matches.append(index)
    if not matches:
        limit = 8_000 if max_chars is None else max(0, max_chars)
        return chunk.annotated_patch[:limit]
    index = matches[0]
    first = max(0, index - radius)
    last = min(len(rows), index + radius + 1)
    excerpt_rows = rows[first:last]
    if max_chars is None:
        return "\n".join(excerpt_rows)
    anchor_index = index - first
    selected = {anchor_index}
    for distance in range(1, radius + 1):
        for candidate in (anchor_index - distance, anchor_index + distance):
            if not 0 <= candidate < len(excerpt_rows):
                continue
            proposed = "\n".join(excerpt_rows[row] for row in sorted((*selected, candidate)))
            if len(proposed) <= max_chars:
                selected.add(candidate)
    rendered = "\n".join(excerpt_rows[row] for row in sorted(selected))
    return rendered[:max_chars]


def _verification_evidence(
    findings: Sequence[Finding],
    chunks: Mapping[str, DiffChunk],
    contexts: Mapping[str, str],
    snapshot: RepositorySnapshot,
    *,
    max_context_chars: int,
    max_patch_chars: int,
    base_sha: str,
    base_paths: Mapping[str, str],
    context_files: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    patch_specs: list[tuple[str, DiffChunk, Finding, str]] = []
    source_specs: list[tuple[str, Finding, str]] = []
    seen_patch: set[tuple[str, str, int]] = set()
    seen_source: set[tuple[str, str, int]] = set()
    for index, finding in enumerate(findings):
        chunk = chunks.get(finding.chunk_id)
        patch_key = (finding.chunk_id, finding.side, finding.line)
        if chunk is not None and patch_key not in seen_patch:
            seen_patch.add(patch_key)
            header = f"### candidate {index}: {display_path(finding.path)}:{finding.line}\n"
            full_excerpt = _anchor_patch_excerpt(chunk, finding.line, finding.side)
            patch_specs.append((header, chunk, finding, full_excerpt))
        source_key = (finding.side, finding.path, finding.line)
        if source_key not in seen_source:
            seen_source.add(source_key)
            try:
                full_excerpt = _line_source(
                    snapshot,
                    finding,
                    base_sha=base_sha,
                    base_path=base_paths.get(finding.path),
                )
            except (OSError, ValueError, RuntimeError):
                full_excerpt = ""
            if full_excerpt:
                source_specs.append(
                    (
                        f"### {finding.side} source: {display_path(finding.path)}:{finding.line}\n",
                        finding,
                        full_excerpt,
                    )
                )

    patch_maximums = [len(header) + len(excerpt) for header, _, _, excerpt in patch_specs]
    patch_full = "\n\n".join(header + excerpt for header, _, _, excerpt in patch_specs)
    patch_allocations = _fair_block_allocations(patch_maximums, max_patch_chars)
    patch_blocks: list[str] = []
    for (header, chunk, finding, _), allocation in zip(patch_specs, patch_allocations, strict=True):
        if allocation <= 0:
            continue
        if allocation <= len(header):
            patch_blocks.append(header[:allocation])
            continue
        patch_blocks.append(
            header
            + _anchor_patch_excerpt(
                chunk,
                finding.line,
                finding.side,
                max_chars=allocation - len(header),
            )
        )

    context_blocks: list[str] = []
    context_headers: list[str] = []
    context_block_files: list[set[str]] = []
    context_positions: dict[str, int] = {}
    for chunk_id in dict.fromkeys(finding.chunk_id for finding in findings):
        value = contexts.get(chunk_id, "")
        context_id = sha256_text(value) if value else ""
        if not value:
            continue
        files = {
            str(path) for path in (context_files or {}).get(chunk_id, ()) if isinstance(path, str)
        }
        if context_id in context_positions:
            context_block_files[context_positions[context_id]].update(files)
            continue
        context_positions[context_id] = len(context_blocks)
        header = f"### generation context: {chunk_id}\n"
        context_headers.append(header)
        context_blocks.append(header + value)
        context_block_files.append(files)

    source_maximums = [len(header) + len(excerpt) for header, _, excerpt in source_specs]
    source_full_chars = sum(source_maximums) + max(0, 2 * (len(source_specs) - 1))
    context_full_chars = len("\n\n".join(context_blocks))

    # If both evidence classes exist, cross-file generation context receives a
    # guaranteed half-share before unused room flows to source excerpts (and
    # vice versa). This prevents many or very long candidate files from
    # suppressing the call-site/contracts evidence selected during generation.
    between_groups = 2 if source_specs and context_blocks else 0
    available = max(0, max_context_chars - between_groups)
    if source_specs and context_blocks:
        context_budget = min(context_full_chars, available // 2)
        source_budget = min(source_full_chars, available - context_budget)
        context_budget = min(context_full_chars, available - source_budget)
    elif context_blocks:
        context_budget = min(context_full_chars, available)
        source_budget = 0
    else:
        context_budget = 0
        source_budget = min(source_full_chars, available)

    context_allocations = _fair_block_allocations(
        [len(block) for block in context_blocks], context_budget
    )
    rendered_context = "\n\n".join(
        block[:allocation]
        for block, allocation in zip(context_blocks, context_allocations, strict=True)
        if allocation > 0
    )
    source_allocations = _fair_block_allocations(source_maximums, source_budget)
    rendered_sources: list[str] = []
    for (header, finding, excerpt), allocation in zip(
        source_specs, source_allocations, strict=True
    ):
        if allocation <= 0:
            continue
        if allocation <= len(header):
            rendered_sources.append(header[:allocation])
            continue
        excerpt_rows = git_lines(excerpt)
        anchor_prefix = f"{finding.line:>6} | "
        anchor_index = next(
            (index for index, row in enumerate(excerpt_rows) if row.startswith(anchor_prefix)),
            0,
        )
        rendered_sources.append(
            header
            + _anchor_centered_rows(
                excerpt_rows,
                anchor_index,
                max_chars=allocation - len(header),
            )
        )

    rendered_source = "\n\n".join(rendered_sources)
    context_and_source = "\n\n".join(
        value for value in (rendered_context, rendered_source) if value
    )
    rendered_patch = "\n\n".join(patch_blocks)
    full_source = "\n\n".join(header + excerpt for header, _, excerpt in source_specs)
    full_context_and_source = "\n\n".join(
        value for value in ("\n\n".join(context_blocks), full_source) if value
    )
    generation_files_available = (
        sorted(set().union(*context_block_files)) if context_block_files else []
    )
    generation_files_rendered = [
        path for path in generation_files_available if _context_exposes_path(rendered_context, path)
    ]
    source_files_available = sorted({finding.path for _, finding, _ in source_specs})
    source_files_rendered = [
        path
        for path in source_files_available
        if _verifier_source_exposes_path(rendered_source, path)
    ]
    metrics: dict[str, Any] = {
        "patch_budget_chars": max_patch_chars,
        "patch_chars_available": len(patch_full),
        "patch_chars_rendered": len(rendered_patch),
        "patch_chars_omitted_by_evidence_budget": max(0, len(patch_full) - len(rendered_patch)),
        "patch_blocks_available": len(patch_specs),
        "patch_blocks_rendered": sum(
            allocation > len(header)
            for (header, _chunk, _finding, _excerpt), allocation in zip(
                patch_specs, patch_allocations, strict=True
            )
        ),
        "context_budget_chars": max_context_chars,
        "context_chars_available": len(full_context_and_source),
        "context_chars_rendered": len(context_and_source),
        "context_chars_omitted_by_evidence_budget": max(
            0, len(full_context_and_source) - len(context_and_source)
        ),
        "generation_context_chars_available": context_full_chars,
        "generation_context_chars_rendered": len(rendered_context),
        "generation_context_chars_omitted": max(0, context_full_chars - len(rendered_context)),
        "generation_context_blocks_available": len(context_blocks),
        "generation_context_blocks_rendered": sum(
            allocation > len(header)
            for header, allocation in zip(context_headers, context_allocations, strict=True)
        ),
        "generation_context_files_available": generation_files_available,
        "generation_context_files_rendered": generation_files_rendered,
        "generation_context_files_omitted": sorted(
            set(generation_files_available) - set(generation_files_rendered)
        ),
        "source_context_chars_available": len(full_source),
        "source_context_chars_rendered": len(rendered_source),
        "source_context_chars_omitted": max(0, len(full_source) - len(rendered_source)),
        "source_blocks_available": len(source_specs),
        "source_blocks_rendered": sum(
            allocation > len(header)
            for (header, _finding, _excerpt), allocation in zip(
                source_specs, source_allocations, strict=True
            )
        ),
        "source_files_available": source_files_available,
        "source_files_rendered": source_files_rendered,
        "source_files_omitted": sorted(set(source_files_available) - set(source_files_rendered)),
    }
    metrics["patch_budget_clipped"] = metrics["patch_chars_omitted_by_evidence_budget"] > 0
    metrics["context_budget_clipped"] = metrics["context_chars_omitted_by_evidence_budget"] > 0
    metrics["evidence_budget_clipped"] = bool(
        metrics["patch_budget_clipped"] or metrics["context_budget_clipped"]
    )
    return rendered_patch, context_and_source, metrics


class ReviewEngine:
    """Lossless diff review with bounded parallel generation and optional verification."""

    def __init__(
        self,
        config: ReviewConfig,
        gateway: ModelGateway,
        repository_cache: GitRepositoryCache,
    ) -> None:
        config.validate()
        if config.verification_batch_size > VERIFIER_MAX_BATCH:
            raise ValueError(f"verification_batch_size cannot exceed {VERIFIER_MAX_BATCH}")
        self.config = config
        self.gateway = gateway
        self.repository_cache = repository_cache

    async def _acquire_snapshot(self, pr: PRInfo) -> RepositorySnapshot:
        """Acquire without leaking a late snapshot when the event loop exits.

        ``asyncio.to_thread`` cannot stop its worker, and an application event
        loop may shut down immediately after the outer review is cancelled.
        Keep the cancellation handoff in state shared with the worker itself so
        cleanup does not depend on an asyncio callback getting another turn.
        """

        state_lock = threading.Lock()
        cancelled = False
        materialized: RepositorySnapshot | None = None

        def acquire_snapshot() -> RepositorySnapshot:
            nonlocal materialized
            snapshot = self.repository_cache.acquire(pr)
            with state_lock:
                dispose_here = cancelled
                if not dispose_here:
                    materialized = snapshot
            if dispose_here:
                # The event loop may already be gone; close in this worker.
                snapshot.close()
            return snapshot

        acquire = asyncio.ensure_future(asyncio.to_thread(acquire_snapshot))
        try:
            return await asyncio.shield(acquire)
        except asyncio.CancelledError:
            with state_lock:
                cancelled = True
                snapshot = materialized
            if snapshot is not None:
                # Acquisition won the race but its result was never delivered.
                # Await cleanup off-loop before acknowledging cancellation; a
                # daemon cleanup thread could be killed during process exit.
                await asyncio.to_thread(snapshot.close)

            # Consume a late failure when the loop remains alive. Worker-side
            # cleanup above still works if shutdown cancels this inner task.
            def _consume_result(task: asyncio.Future[RepositorySnapshot]) -> None:
                if not task.cancelled():
                    task.exception()

            acquire.add_done_callback(_consume_result)
            raise

    async def review(self, pr: PRInfo) -> ReviewArtifact:
        started_at = utc_now()
        started_ms = monotonic_ms()
        runtime = review_runtime_provenance(
            self.gateway,
            model=self.config.model,
            verifier_model=self.config.verifier_model,
            generation_reasoning_effort=self.config.reasoning_effort,
            verifier_reasoning_effort=self.config.verifier_reasoning_effort,
            generation_max_output_tokens=self.config.max_output_tokens,
            verifier_max_output_tokens=self.config.verifier_max_output_tokens,
        )
        implementation = implementation_identity()
        run_id = sha256_text(
            canonical_json(
                {
                    "tool": "bugbunny",
                    "version": __version__,
                    "implementation": implementation,
                    "started_at": started_at,
                    "base": pr.base_sha,
                    "head": pr.head_sha,
                    "config": self.config.to_dict(),
                    "runtime": runtime,
                }
            )
        )[:24]
        snapshot: RepositorySnapshot | None = None
        parsed: ParsedDiff | None = None
        plan: Any = None
        batches: tuple[_GenerationBatch, ...] = ()
        bundle: Any = None
        calls: list[CallRecord] = []
        raw_findings: list[Finding] = []
        validated_findings: list[Finding] = []
        rejected: list[RejectedFinding] = []
        findings: list[Finding] = []
        diagnostics: list[dict[str, Any]] = []
        context_selection: dict[str, dict[str, Any]] = {}
        batch_context_metrics: dict[str, dict[str, Any]] = {}
        context_files_by_chunk: dict[str, tuple[str, ...]] = {}
        verification_context_metrics: list[dict[str, Any]] = []
        completed_chunks: set[str] = set()
        failed_chunks: set[str] = set()
        raw_diff = ""
        eligible_changed_lines: dict[str, set[int]] = {}
        eligible_deleted_lines: dict[str, set[int]] = {}
        review_base_sha = pr.base_sha
        fatal = False

        try:
            snapshot = await self._acquire_snapshot(pr)
            raw_diff = await asyncio.to_thread(snapshot.diff, self.config.diff_context_lines)
            parsed = parse_unified_diff(raw_diff)
            plan = parsed.chunk(self.config.max_chunk_chars)
            base_path_for_review_path = {
                file_diff.path: file_diff.old_path or file_diff.path
                for file_diff in parsed.files
                if file_diff.exclusion is None
            }
            left_path_aliases = {
                old_path: review_path
                for review_path, old_path in base_path_for_review_path.items()
                if old_path
            }
            review_base_sha = getattr(snapshot, "review_base_sha", pr.base_sha)
            seed_config = (
                self.config
                if self.config.context_mode == "curated"
                else replace(
                    self.config,
                    max_context_chars=min(
                        self.config.initial_context_chars,
                        self.config.max_context_chars,
                    ),
                )
            )
            bundle = await asyncio.to_thread(
                ContextBuilder(snapshot, seed_config, pr=pr).build,
                parsed,
                plan,
            )
            contexts = {
                chunk.chunk_id: _context_prompt(bundle, chunk.chunk_id) for chunk in plan.chunks
            }
            initial_batch_context_chars = (
                self.config.max_context_chars
                if self.config.context_mode == "curated"
                else min(self.config.initial_context_chars, self.config.max_context_chars)
            )
            batches = _generation_batches(
                plan.chunks,
                contexts,
                max_patch_chars=self.config.max_chunk_chars,
                max_context_chars=initial_batch_context_chars,
            )
            semaphore = asyncio.Semaphore(self.config.llm_concurrency)
            bounded_gateway = _SemaphoreGateway(self.gateway, semaphore)
            # One ledger for the whole review: context_blob_read_bytes is
            # documented as cumulative across agentic reads for the review,
            # not per generation batch.
            shared_blob_budget = SharedBlobBudget(self.config.context_blob_read_bytes)
            file_inventory = (
                tuple(sorted(await asyncio.to_thread(snapshot.list_files, snapshot.head_sha)))
                if self.config.context_mode == "agentic"
                else ()
            )

            async def review_batch(
                batch: _GenerationBatch,
            ) -> tuple[
                _GenerationBatch,
                list[Finding],
                tuple[CallRecord, ...],
                str | None,
                str | None,
                dict[str, Any],
                tuple[dict[str, str], ...],
            ]:
                batch_calls: list[CallRecord] = []
                trace: dict[str, Any] = {
                    "mode": "curated",
                    "round_limit": 0,
                    "rounds_completed": 0,
                    "final_context_chars": len(batch.context),
                    "context_tokens_estimated": (len(batch.context) + 3) // 4,
                    "context_truncated": any(
                        bool(getattr(bundle.by_chunk.get(chunk.chunk_id), "truncated", False))
                        for chunk in batch.chunks
                    ),
                    "context_files_exposed_to_model": [],
                }
                selected_diagnostics: tuple[dict[str, str], ...] = ()
                final_batch = batch
                if self.config.context_mode == "agentic":
                    try:
                        selection = await explore_repository_context(
                            config=self.config,
                            model=self.config.model,
                            gateway=bounded_gateway,
                            snapshot=snapshot,
                            batch_patch=batch.patch,
                            seed_context=batch.context,
                            file_inventory=file_inventory,
                            batch_id=batch.batch_id,
                            blob_budget=shared_blob_budget,
                        )
                    except Exception as exc:
                        return (
                            batch,
                            [],
                            (),
                            _safe_error(exc),
                            "context_selection",
                            {"mode": "agentic", "failed": True},
                            (),
                        )
                    batch_calls.extend(selection.calls)
                    trace = dict(selection.trace)
                    selected_diagnostics = selection.diagnostics
                    final_batch = replace(batch, context=selection.context)
                    if bool(getattr(selection, "failed", trace.get("failed", False))):
                        return (
                            final_batch,
                            [],
                            tuple(batch_calls),
                            "model-directed context selection failed",
                            "context_selection",
                            trace,
                            selected_diagnostics,
                        )
                try:
                    prompt, final_batch, prompt_context_clipped = _fit_generation_prompt(
                        final_batch,
                        max_input_chars=self.config.generation_input_char_budget,
                        pr_title=pr.title,
                        pr_body=pr.body,
                        allowed_categories=self.config.include_categories,
                        review_policy=self.config.review_policy,
                    )
                except Exception as exc:
                    return (
                        final_batch,
                        [],
                        tuple(batch_calls),
                        _safe_error(exc),
                        "generation",
                        trace,
                        selected_diagnostics,
                    )
                selected_paths = trace.get("context_files_exposed_to_model", ())
                if isinstance(selected_paths, Sequence) and not isinstance(
                    selected_paths, (str, bytes)
                ):
                    effective_selected_paths = sorted(
                        {
                            str(path)
                            for path in selected_paths
                            if _context_exposes_path(final_batch.context, str(path))
                        }
                    )
                else:
                    effective_selected_paths = []
                selected_paths_before_fit = (
                    sorted({str(path) for path in selected_paths})
                    if isinstance(selected_paths, Sequence)
                    and not isinstance(selected_paths, (str, bytes))
                    else []
                )
                trace = {
                    **trace,
                    "context_files_before_generation_prompt_fit": selected_paths_before_fit,
                    "context_files_exposed_to_model": effective_selected_paths,
                    "context_files_omitted_by_generation_prompt_fit": sorted(
                        set(selected_paths_before_fit) - set(effective_selected_paths)
                    ),
                    "unique_context_files": len(effective_selected_paths),
                    "final_context_chars": len(final_batch.context),
                    "generation_prompt_chars": len(prompt),
                    "generation_prompt_utf8_bytes": len(prompt.encode("utf-8")),
                    "generation_input_char_budget": self.config.generation_input_char_budget,
                    "generation_input_char_budget_utilization": (
                        len(prompt) / self.config.generation_input_char_budget
                        if self.config.generation_input_char_budget is not None
                        else None
                    ),
                    "generation_context_clipped_to_prompt_budget": prompt_context_clipped,
                    "context_truncated": bool(trace.get("context_truncated", False))
                    or prompt_context_clipped,
                }
                try:
                    result = await bounded_gateway.complete_json(
                        prompt,
                        model=self.config.model,
                        stage="generation",
                        schema_name="bugbunny_findings",
                        schema=GENERATION_TRANSPORT_SCHEMA,
                        chunk_id=final_batch.batch_id,
                        reasoning_effort=self.config.reasoning_effort,
                        max_output_tokens=self.config.max_output_tokens,
                        operation_timeout_seconds=_operation_deadline_seconds(
                            self.config.timeout_seconds
                        ),
                    )
                except GatewayError as exc:
                    batch_calls.append(exc.call)
                    return (
                        final_batch,
                        [],
                        tuple(batch_calls),
                        str(exc),
                        "generation",
                        trace,
                        selected_diagnostics,
                    )
                except Exception as exc:  # defensive transport boundary
                    return (
                        final_batch,
                        [],
                        tuple(batch_calls),
                        _safe_error(exc),
                        "generation",
                        trace,
                        selected_diagnostics,
                    )
                batch_calls.append(result.call)
                try:
                    proposed, invalid_finding_count = findings_from_payload_tolerant(
                        result.payload,
                        chunk_id=final_batch.batch_id,
                    )
                    if invalid_finding_count:
                        selected_diagnostics = (
                            *selected_diagnostics,
                            {
                                "stage": "generation_payload",
                                "code": "invalid_findings_quarantined",
                                "count": str(invalid_finding_count),
                            },
                        )
                    for finding in proposed:
                        _assign_source_chunk(
                            finding,
                            final_batch,
                            left_path_aliases=left_path_aliases,
                        )
                except Exception as exc:
                    return (
                        final_batch,
                        [],
                        tuple(batch_calls),
                        _safe_error(exc),
                        "generation",
                        trace,
                        selected_diagnostics,
                    )
                return (
                    final_batch,
                    proposed,
                    tuple(batch_calls),
                    None,
                    None,
                    trace,
                    selected_diagnostics,
                )

            generated = await asyncio.gather(*(review_batch(batch) for batch in batches))
            for generated_batch in generated:
                (
                    batch,
                    proposed,
                    batch_calls,
                    error,
                    error_stage,
                    trace,
                    selection_diagnostics,
                ) = generated_batch
                calls.extend(batch_calls)
                context_selection[batch.batch_id] = trace
                for item in selection_diagnostics:
                    diagnostics.append(
                        {
                            **item,
                            "batch_id": batch.batch_id,
                            "chunk_ids": [chunk.chunk_id for chunk in batch.chunks],
                        }
                    )
                packet_files_available: set[str] = set()
                seed_packets_truncated = False
                for chunk in batch.chunks:
                    packet = getattr(bundle, "by_chunk", {}).get(chunk.chunk_id)
                    seed_packets_truncated = seed_packets_truncated or bool(
                        getattr(packet, "truncated", False)
                    )
                    telemetry = getattr(packet, "telemetry", {})
                    if isinstance(telemetry, Mapping):
                        packet_files_available.update(
                            str(path)
                            for path in telemetry.get("context_files_exposed_to_model", ())
                        )
                selected_files = trace.get(
                    "context_files_before_generation_prompt_fit",
                    trace.get("context_files_exposed_to_model", ()),
                )
                if isinstance(selected_files, Sequence) and not isinstance(
                    selected_files, (str, bytes)
                ):
                    packet_files_available.update(str(path) for path in selected_files)
                packet_files = {
                    path
                    for path in packet_files_available
                    if _context_exposes_path(batch.context, path)
                }
                batch_context_metrics[batch.batch_id] = {
                    "chunk_ids": [chunk.chunk_id for chunk in batch.chunks],
                    "context_chars": len(batch.context),
                    "context_utf8_bytes": len(batch.context.encode("utf-8")),
                    "estimated_context_tokens": (len(batch.context) + 3) // 4,
                    "estimated_context_tokens_method": "ceil(Unicode characters / 4); estimate, not tokenizer output",
                    "context_budget_chars": self.config.max_context_chars,
                    "context_budget_utilization": len(batch.context)
                    / self.config.max_context_chars,
                    "context_sha256": sha256_text(batch.context),
                    "context_files_exposed_to_model": sorted(packet_files),
                    "unique_context_files_exposed_to_model": len(packet_files),
                    "context_files_available_before_generation_prompt_fit": sorted(
                        packet_files_available
                    ),
                    "context_files_omitted_by_generation_prompt_fit": sorted(
                        packet_files_available - packet_files
                    ),
                    "seed_packets_truncated": seed_packets_truncated,
                    "context_truncated": seed_packets_truncated
                    or bool(trace.get("context_truncated", False)),
                    "generation_prompt_chars": trace.get("generation_prompt_chars"),
                    "generation_prompt_utf8_bytes": trace.get("generation_prompt_utf8_bytes"),
                    "generation_input_char_budget": trace.get("generation_input_char_budget"),
                    "generation_input_char_budget_utilization": trace.get(
                        "generation_input_char_budget_utilization"
                    ),
                    "generation_context_clipped_to_prompt_budget": bool(
                        trace.get("generation_context_clipped_to_prompt_budget", False)
                    ),
                }
                for chunk in batch.chunks:
                    context_files_by_chunk[chunk.chunk_id] = tuple(sorted(packet_files))
                if self.config.context_mode == "agentic" and error_stage != "context_selection":
                    for chunk in batch.chunks:
                        contexts[chunk.chunk_id] = batch.context
                if error is not None:
                    failed_chunks.update(chunk.chunk_id for chunk in batch.chunks)
                    diagnostics.append(
                        {
                            "stage": error_stage or "generation",
                            "batch_id": batch.batch_id,
                            "chunk_ids": [chunk.chunk_id for chunk in batch.chunks],
                            "error": error,
                        }
                    )
                    continue
                completed_chunks.update(chunk.chunk_id for chunk in batch.chunks)
                raw_findings.extend(proposed)

            for chunk in plan.chunks:
                eligible_changed_lines.setdefault(chunk.path, set()).update(chunk.added_lines)
                eligible_deleted_lines.setdefault(chunk.path, set()).update(chunk.deleted_lines)
            chunk_locations = {
                chunk.chunk_id: (
                    chunk.path,
                    frozenset(chunk.added_lines),
                    frozenset(chunk.deleted_lines),
                )
                for chunk in plan.chunks
            }
            # Deterministic grounding reads sources through git subprocesses;
            # run it off the event loop so concurrent reviews keep flowing.
            validated, validation_rejections = await asyncio.to_thread(
                validate_findings,
                raw_findings,
                changed_lines=eligible_changed_lines,
                read_source=snapshot.read_text,
                deleted_lines=eligible_deleted_lines,
                read_base_source=lambda path: snapshot.read_blob(
                    review_base_sha, base_path_for_review_path.get(path, path)
                ),
                chunk_locations=chunk_locations,
                config=self.config,
                base_sha=review_base_sha,
                head_sha=pr.head_sha,
            )
            rejected.extend(validation_rejections)
            validated_findings = validated

            verifier_model = self.config.verifier_model
            if verifier_model == "same":
                verifier_model = self.config.model
            if validated and verifier_model not in {None, "none"}:
                chunks_by_id = {chunk.chunk_id: chunk for chunk in plan.chunks}
                verified: list[Finding] = []
                verifier_failed = False
                verifier_batches = _verification_batches(
                    validated,
                    max_items=self.config.verification_batch_size,
                    max_chars=self.config.verification_batch_chars,
                    max_prompt_chars=self.config.verifier_input_char_budget,
                )
                for offset, batch in verifier_batches:
                    semantic_retry_errors: list[str] = []
                    try:
                        verifier_limit = self.config.verifier_input_char_budget
                        if verifier_limit is None:
                            verifier_patch_budget = self.config.max_chunk_chars
                            verifier_context_budget = self.config.max_context_chars
                        else:
                            candidate_prompt_chars = len(
                                build_verifier_prompt(
                                    batch,
                                    "",
                                    "",
                                    max_batch_size=self.config.verification_batch_size,
                                )
                            )
                            evidence_chars = max(
                                0,
                                verifier_limit - candidate_prompt_chars - 512,
                            )
                            verifier_patch_budget = min(
                                self.config.max_chunk_chars,
                                max(1, evidence_chars * 2 // 5),
                            )
                            verifier_context_budget = min(
                                self.config.max_context_chars,
                                max(1, evidence_chars - verifier_patch_budget),
                            )
                        patch, context, evidence_metrics = await asyncio.to_thread(
                            _verification_evidence,
                            batch,
                            chunks_by_id,
                            contexts,
                            snapshot,
                            max_context_chars=verifier_context_budget,
                            max_patch_chars=verifier_patch_budget,
                            base_sha=review_base_sha,
                            base_paths=base_path_for_review_path,
                            context_files=context_files_by_chunk,
                        )
                        context_before_prompt_fit = context
                        prompt, context, verifier_context_clipped = _fit_verifier_prompt(
                            batch,
                            patch,
                            context,
                            max_batch_size=self.config.verification_batch_size,
                            max_input_chars=verifier_limit,
                        )
                        generation_files_available = evidence_metrics.get(
                            "generation_context_files_available", ()
                        )
                        source_files_available = evidence_metrics.get("source_files_available", ())
                        verifier_generation_context = _verifier_generation_context(context)
                        verification_metrics: dict[str, Any] = {
                            **evidence_metrics,
                            "candidate_offset": offset,
                            "candidate_count": len(batch),
                            "candidate_payload_chars": len(verifier_candidate_payload(batch)),
                            "patch_chars": len(patch),
                            "context_chars": len(context),
                            "context_chars_before_prompt_fit": len(context_before_prompt_fit),
                            "context_chars_omitted_by_prompt_fit": max(
                                0, len(context_before_prompt_fit) - len(context)
                            ),
                            "generation_context_files_after_prompt_fit": [
                                path
                                for path in generation_files_available
                                if _context_exposes_path(verifier_generation_context, str(path))
                            ],
                            "source_files_after_prompt_fit": [
                                path
                                for path in source_files_available
                                if _verifier_source_exposes_path(context, str(path))
                            ],
                            "prompt_chars": len(prompt),
                            "prompt_utf8_bytes": len(prompt.encode("utf-8")),
                            "input_char_budget": verifier_limit,
                            "input_char_budget_utilization": (
                                len(prompt) / verifier_limit if verifier_limit is not None else None
                            ),
                            "context_clipped_to_prompt_budget": verifier_context_clipped,
                            "context_clipped_to_any_budget": bool(
                                evidence_metrics.get("context_budget_clipped")
                                or verifier_context_clipped
                            ),
                            "evidence_clipped_to_any_budget": bool(
                                evidence_metrics.get("evidence_budget_clipped")
                                or verifier_context_clipped
                            ),
                            "attempts": [],
                        }
                        verification_context_metrics.append(verification_metrics)
                        decisions: list[dict[str, Any]] | None = None
                        semantic_attempt_prompt = prompt
                        semantic_attempt_context = context
                        semantic_attempt_context_clipped = verifier_context_clipped
                        semantic_retry_notice = ""
                        for semantic_attempt in range(
                            self.config.verification_semantic_retries + 1
                        ):
                            attempt_generation_context = _verifier_generation_context(
                                semantic_attempt_context
                            )
                            attempt_metrics = {
                                "semantic_attempt": semantic_attempt,
                                "is_retry": semantic_attempt > 0,
                                "retry_notice_chars": len(semantic_retry_notice),
                                "prompt_chars": len(semantic_attempt_prompt),
                                "prompt_utf8_bytes": len(semantic_attempt_prompt.encode("utf-8")),
                                "input_char_budget": verifier_limit,
                                "input_char_budget_utilization": (
                                    len(semantic_attempt_prompt) / verifier_limit
                                    if verifier_limit is not None
                                    else None
                                ),
                                "context_chars": len(semantic_attempt_context),
                                "context_chars_omitted_by_prompt_fit": max(
                                    0,
                                    len(context_before_prompt_fit) - len(semantic_attempt_context),
                                ),
                                "context_clipped_to_prompt_budget": (
                                    semantic_attempt_context_clipped
                                ),
                                "generation_context_files_after_prompt_fit": [
                                    path
                                    for path in generation_files_available
                                    if _context_exposes_path(attempt_generation_context, str(path))
                                ],
                                "source_files_after_prompt_fit": [
                                    path
                                    for path in source_files_available
                                    if _verifier_source_exposes_path(
                                        semantic_attempt_context, str(path)
                                    )
                                ],
                            }
                            verification_metrics["attempts"].append(attempt_metrics)
                            # Batch-level evidence fields describe the last
                            # actual (therefore decisive or terminal) attempt;
                            # the full history remains under ``attempts``.
                            verification_metrics.update(
                                {
                                    "prompt_chars": attempt_metrics["prompt_chars"],
                                    "prompt_utf8_bytes": attempt_metrics["prompt_utf8_bytes"],
                                    "input_char_budget_utilization": attempt_metrics[
                                        "input_char_budget_utilization"
                                    ],
                                    "context_chars": attempt_metrics["context_chars"],
                                    "context_chars_omitted_by_prompt_fit": attempt_metrics[
                                        "context_chars_omitted_by_prompt_fit"
                                    ],
                                    "generation_context_files_after_prompt_fit": attempt_metrics[
                                        "generation_context_files_after_prompt_fit"
                                    ],
                                    "source_files_after_prompt_fit": attempt_metrics[
                                        "source_files_after_prompt_fit"
                                    ],
                                    "context_clipped_to_prompt_budget": (
                                        semantic_attempt_context_clipped
                                    ),
                                    "context_clipped_to_any_budget": bool(
                                        evidence_metrics.get("context_budget_clipped")
                                        or semantic_attempt_context_clipped
                                    ),
                                    "evidence_clipped_to_any_budget": bool(
                                        evidence_metrics.get("evidence_budget_clipped")
                                        or semantic_attempt_context_clipped
                                    ),
                                }
                            )
                            result = await self.gateway.complete_json(
                                semantic_attempt_prompt,
                                model=verifier_model,
                                stage="verification",
                                schema_name="bugbunny_verification",
                                schema=VERIFIER_SCHEMA,
                                chunk_id=f"candidates-{offset}-{offset + len(batch) - 1}",
                                reasoning_effort=self.config.verifier_reasoning_effort,
                                max_output_tokens=self.config.verifier_max_output_tokens,
                                operation_timeout_seconds=_operation_deadline_seconds(
                                    self.config.timeout_seconds
                                ),
                            )
                            calls.append(result.call)
                            try:
                                decisions = validate_verifier_payload(
                                    result.payload, candidate_count=len(batch)
                                )
                            except PayloadValidationError as exc:
                                semantic_error = _safe_error(exc)
                                calls[-1] = replace(result.call, error=semantic_error)
                                semantic_retry_errors.append(semantic_error)
                                if semantic_attempt < self.config.verification_semantic_retries:
                                    semantic_retry_notice = (
                                        "\n\nVerifier retry notice\n"
                                        + "The previous response was rejected by the semantic "
                                        + "contract: "
                                        + semantic_error
                                        + ". Regenerate the complete decisions array and correct "
                                        + "that relationship; do not omit any candidate."
                                    )
                                    (
                                        semantic_attempt_prompt,
                                        semantic_attempt_context,
                                        semantic_attempt_context_clipped,
                                    ) = _fit_verifier_prompt(
                                        batch,
                                        patch,
                                        context_before_prompt_fit,
                                        max_batch_size=self.config.verification_batch_size,
                                        max_input_chars=verifier_limit,
                                        retry_notice=semantic_retry_notice,
                                    )
                                    continue
                                raise
                            break
                        if decisions is None:
                            raise ReviewEngineError("verifier semantic retry loop did not resolve")
                        kept, dropped = apply_verifier_decisions(
                            batch,
                            {"decisions": decisions},
                            min_confidence=self.config.min_verifier_confidence,
                        )
                    except GatewayError as exc:
                        calls.append(exc.call)
                        error = str(exc)
                        verifier_failed = True
                    except Exception as exc:
                        error = _safe_error(exc)
                        verifier_failed = True
                    else:
                        if semantic_retry_errors:
                            diagnostics.append(
                                {
                                    "stage": "verification_semantic_retry",
                                    "candidate_offset": offset,
                                    "retry_count": len(semantic_retry_errors),
                                    "recovered": True,
                                    "errors": semantic_retry_errors,
                                }
                            )
                        verified.extend(kept)
                        rejected.extend(dropped)
                        continue

                    diagnostics.append(
                        {
                            "stage": "verification",
                            "candidate_offset": offset,
                            "error": error,
                            "failure_policy": "fail_closed",
                            "semantic_attempt_count": len(semantic_retry_errors),
                            "semantic_retry_count": max(0, len(semantic_retry_errors) - 1),
                        }
                    )
                    rejected.extend(
                        RejectedFinding(item, "verifier_error", error) for item in batch
                    )
                    rejected.extend(
                        RejectedFinding(
                            item,
                            "verifier_run_failed",
                            "a later verifier batch failed; global fail-closed policy",
                        )
                        for item in verified
                    )
                    remaining = validated[offset + len(batch) :]
                    rejected.extend(
                        RejectedFinding(
                            item,
                            "verifier_skipped",
                            "not attempted after an earlier verifier batch failed",
                        )
                        for item in remaining
                    )
                    break
                if verifier_failed:
                    findings = []
                else:
                    findings, semantic_duplicates = consolidate_semantic_duplicates(verified)
                    rejected.extend(semantic_duplicates)
                fatal = verifier_failed
            else:
                findings = validated

            await asyncio.to_thread(snapshot.assert_clean)
        except Exception as exc:
            fatal = True
            diagnostics.append({"stage": "engine", "error": _safe_error(exc)})
        finally:
            if snapshot is not None:
                try:
                    await asyncio.to_thread(snapshot.close)
                except Exception as exc:
                    fatal = True
                    diagnostics.append({"stage": "snapshot_close", "error": _safe_error(exc)})

        eligible_hunks: list[str] = []
        all_hunks: list[str] = []
        exclusions: list[dict[str, str]] = []
        total_files = eligible_files = 0
        if parsed is not None:
            total_files = len(parsed.files)
            all_hunks = [hunk.hunk_id for hunk in parsed.hunks]
            eligible_hunks = [
                hunk.hunk_id
                for file_diff in parsed.files
                if file_diff.exclusion is None
                for hunk in file_diff.hunks
            ]
            exclusions = [
                {"path": item.path, "kind": item.kind, "reason": item.reason}
                for item in parsed.exclusions
            ]
            eligible_files = sum(item.exclusion is None for item in parsed.files)

        chunks_for_hunk: dict[str, set[str]] = {hunk_id: set() for hunk_id in eligible_hunks}
        if plan is not None:
            for chunk in plan.chunks:
                for hunk_id in chunk.hunk_ids:
                    chunks_for_hunk.setdefault(hunk_id, set()).add(chunk.chunk_id)
        completed_hunks = [
            hunk_id
            for hunk_id, chunk_ids in chunks_for_hunk.items()
            if chunk_ids and chunk_ids <= completed_chunks
        ]
        failed_hunks = [
            hunk_id
            for hunk_id, chunk_ids in chunks_for_hunk.items()
            if hunk_id not in completed_hunks
        ]
        coverage = Coverage(
            total_files=total_files,
            eligible_files=eligible_files,
            excluded_files=exclusions,
            total_hunks=len(all_hunks),
            eligible_hunks=len(eligible_hunks),
            completed_hunks=completed_hunks,
            failed_hunks=failed_hunks,
            eligible_hunk_ids=list(eligible_hunks),
        )
        if fatal:
            status = "failed"
        elif coverage.complete:
            status = "completed"
        elif completed_hunks:
            status = "partial"
        else:
            status = "failed"

        completed_at = utc_now()
        context_summary = (
            _context_summary(bundle, review_policy=self.config.review_policy)
            if bundle is not None
            else {
                "generation_prompt_version": GENERATION_PROMPT_VERSION,
                "generation_prompt_sha256": generation_prompt_sha256(self.config.review_policy),
                "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
                "verifier_prompt_sha256": verifier_prompt_sha256(),
                "context_selection_prompt_version": EXPLORATION_PROMPT_VERSION,
                "context_selection_prompt_sha256": exploration_prompt_sha256(),
                "context_selection_schema_version": EXPLORATION_SCHEMA_VERSION,
            }
        )
        context_summary["context_selection_schema_sha256"] = exploration_schema_sha256(
            self.config.context_requests_per_round,
            self.config.context_search_max_offset,
        )
        context_summary["mode"] = self.config.context_mode
        context_summary["budget"] = {
            "source": self.config.context_budget_source,
            "verifier_source": (
                "declared_window"
                if self.config.verifier_context_window_tokens is not None
                else "fixed"
            ),
            "declared_model_context_window_tokens": self.config.context_window_tokens,
            "max_chunk_chars_per_generation_call": self.config.max_chunk_chars,
            "max_context_chars_per_generation_call": self.config.max_context_chars,
            "initial_context_chars_per_generation_call": (
                self.config.max_context_chars
                if self.config.context_mode == "curated"
                else min(self.config.initial_context_chars, self.config.max_context_chars)
            ),
            "max_output_tokens_per_call": self.config.max_output_tokens,
            "generation_input_char_budget": self.config.generation_input_char_budget,
            "declared_verifier_context_window_tokens": (self.config.verifier_context_window_tokens),
            "verifier_input_char_budget": self.config.verifier_input_char_budget,
            "verifier_max_output_tokens_per_call": (self.config.verifier_max_output_tokens),
            "declared_window_reserve_tokens": (
                DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
                if self.config.context_window_tokens is not None
                else None
            ),
            "declared_window_input_character_assumption": (
                f"{DECLARED_WINDOW_CHARS_PER_TOKEN} Unicode characters/token (planning "
                "estimate, not a tokenizer or hard token guarantee; provider usage is "
                "authoritative)"
                if self.config.context_window_tokens is not None
                else None
            ),
            "declared_verifier_window_reserve_tokens": (
                DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
                if self.config.verifier_context_window_tokens is not None
                else None
            ),
            "declared_verifier_window_input_character_assumption": (
                f"{DECLARED_WINDOW_CHARS_PER_TOKEN} Unicode characters/token (planning "
                "estimate, not a tokenizer or hard token guarantee; provider usage is "
                "authoritative)"
                if self.config.verifier_context_window_tokens is not None
                else None
            ),
        }
        context_summary["generation_batches"] = batch_context_metrics
        context_summary["verification_batches"] = verification_context_metrics
        context_summary["selection"] = {
            "batches": context_selection,
            "failed_batches": sorted(
                batch_id
                for batch_id, trace in context_selection.items()
                if bool(trace.get("failed", False))
            ),
        }
        effective_context_files = sorted(
            {
                path
                for metrics in batch_context_metrics.values()
                for path in metrics.get("context_files_exposed_to_model", ())
            }
        )
        changed_paths = {chunk.path for chunk in plan.chunks} if plan is not None else set()
        context_summary["effective_context_files_exposed_to_model"] = effective_context_files
        context_summary["unique_effective_context_files_exposed_to_model"] = len(
            effective_context_files
        )
        context_summary["unique_changed_context_files_exposed_to_model"] = len(
            set(effective_context_files) & changed_paths
        )
        context_summary["unique_unchanged_context_files_exposed_to_model"] = len(
            set(effective_context_files) - changed_paths
        )
        context_summary["context_pressure"] = {
            "generation_batches": len(batch_context_metrics),
            "batches_marked_truncated": sum(
                bool(metrics.get("context_truncated")) for metrics in batch_context_metrics.values()
            ),
            "batches_at_or_above_95_percent_of_context_budget": sum(
                float(metrics.get("context_budget_utilization", 0.0)) >= 0.95
                for metrics in batch_context_metrics.values()
            ),
            "largest_context_budget_utilization": max(
                (
                    float(metrics.get("context_budget_utilization", 0.0))
                    for metrics in batch_context_metrics.values()
                ),
                default=0.0,
            ),
            "generation_contexts_clipped_to_prompt_budget": sum(
                bool(trace.get("generation_context_clipped_to_prompt_budget"))
                for trace in context_selection.values()
            ),
            "selection_batches_hitting_any_bound": sum(
                any(
                    bool(trace.get(name))
                    for name in (
                        "round_limit_hit",
                        "request_limit_hit",
                        "file_limit_hit",
                        "context_limit_hit",
                        "blob_read_limit_hit",
                        "selector_observations_truncated",
                        "repository_index_truncated",
                        "repository_inventory_omission_hit",
                        "search_pagination_unresolved",
                        "list_pagination_unresolved",
                        "search_offset_limit_hit",
                        "search_scan_limit_hit",
                    )
                )
                for trace in context_selection.values()
            ),
            "selection_bound_hits": {
                name: sum(bool(trace.get(name)) for trace in context_selection.values())
                for name in (
                    "round_limit_hit",
                    "request_limit_hit",
                    "file_limit_hit",
                    "context_limit_hit",
                    "search_hit_limit_hit",
                    "list_page_limit_hit",
                    "blob_read_limit_hit",
                    "selector_observations_truncated",
                    "repository_index_truncated",
                    "repository_inventory_omission_hit",
                    "search_pagination_unresolved",
                    "list_pagination_unresolved",
                    "search_offset_limit_hit",
                    "search_scan_limit_hit",
                )
            },
            "selection_batches_encountering_page_caps": sum(
                bool(trace.get("search_hit_limit_hit")) or bool(trace.get("list_page_limit_hit"))
                for trace in context_selection.values()
            ),
            "selection_batches_encountering_request_caps": sum(
                bool(trace.get("request_cap_reached")) for trace in context_selection.values()
            ),
            "selection_batches_with_selector_observation_clipping": sum(
                bool(trace.get("selector_observations_truncated"))
                for trace in context_selection.values()
            ),
            "selector_inventory_files_omitted": max(
                (
                    int(trace.get("repository_files_omitted_from_selector_inventory", 0))
                    for trace in context_selection.values()
                ),
                default=0,
            ),
            "selection_batches_with_inventory_omissions": sum(
                bool(trace.get("repository_inventory_omission_hit"))
                for trace in context_selection.values()
            ),
            "verifier_contexts_clipped_to_prompt_budget": sum(
                bool(metrics.get("context_clipped_to_prompt_budget"))
                for metrics in verification_context_metrics
            ),
            "verifier_contexts_clipped_to_evidence_budget": sum(
                bool(metrics.get("context_budget_clipped"))
                for metrics in verification_context_metrics
            ),
            "verifier_evidence_batches_clipped_to_any_budget": sum(
                bool(metrics.get("evidence_clipped_to_any_budget"))
                for metrics in verification_context_metrics
            ),
            "verifier_generation_context_files_available": len(
                {
                    path
                    for metrics in verification_context_metrics
                    for path in metrics.get("generation_context_files_available", ())
                }
            ),
            "verifier_generation_context_files_after_prompt_fit": len(
                {
                    path
                    for metrics in verification_context_metrics
                    for path in metrics.get("generation_context_files_after_prompt_fit", ())
                }
            ),
            "largest_verifier_input_char_budget_utilization": max(
                (
                    float(attempt["input_char_budget_utilization"])
                    for metrics in verification_context_metrics
                    for attempt in metrics.get("attempts", ())
                    if attempt.get("input_char_budget_utilization") is not None
                ),
                default=0.0,
            ),
        }
        context_summary["provider_reported_prompt_usage_by_stage"] = _call_token_summary(calls)
        context_summary["pr_metadata"] = generation_metadata_provenance(pr.title, pr.body)
        return ReviewArtifact(
            schema_version=REVIEW_SCHEMA_VERSION,
            tool="bugbunny",
            tool_version=__version__,
            implementation=implementation,
            run_id=run_id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=monotonic_ms() - started_ms,
            pr=pr,
            config=self.config,
            runtime=runtime,
            diff={
                "sha256": sha256_text(raw_diff),
                "merge_base_sha": (
                    getattr(snapshot, "merge_base_sha", None) if snapshot is not None else None
                ),
                "bytes": len(raw_diff.encode("utf-8")),
                "files": total_files,
                "hunks": len(all_hunks),
                "additions": parsed.added_lines if parsed is not None else 0,
                "deletions": parsed.deleted_lines if parsed is not None else 0,
                "chunks": len(plan.chunks) if plan is not None else 0,
                "generation_batches": len(batches) if plan is not None else 0,
                "chunk_plan_complete": bool(plan is not None and plan.complete),
                "commentable_ranges": {
                    "RIGHT": changed_line_ranges(eligible_changed_lines),
                    "LEFT": changed_line_ranges(eligible_deleted_lines),
                },
            },
            coverage=coverage,
            context=context_summary,
            calls=calls,
            raw_findings=raw_findings,
            validated_findings=validated_findings,
            rejected_findings=rejected,
            findings=findings,
            diagnostics=diagnostics,
        )


def write_review_artifact(
    artifact: ReviewArtifact,
    output_path: Path | str,
    markdown_path: Path | str | None = None,
) -> tuple[Path, Path | None]:
    """Atomically persist native JSON and, optionally, a readable report."""

    json_path = Path(output_path).expanduser().resolve()
    atomic_write_json(json_path, artifact.to_dict())
    rendered_path: Path | None = None
    if markdown_path is not None:
        rendered_path = Path(markdown_path).expanduser().resolve()
        atomic_write_text(rendered_path, render_markdown(artifact.to_dict()))
    return json_path, rendered_path


__all__ = [
    "ReviewEngine",
    "ReviewEngineError",
    "review_runtime_provenance",
    "write_review_artifact",
]
