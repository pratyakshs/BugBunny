from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bugbunny.engine as engine_module
from bugbunny.diff import parse_unified_diff
from bugbunny.engine import ReviewEngine, write_review_artifact
from bugbunny.gateway import GatewayConfig, GatewayError, GatewayResult, ModelGateway
from bugbunny.models import CallRecord, Finding, PRInfo, ReviewConfig

TWO_FILE_DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old_a = 0
+new_a = 1
diff --git a/b.py b/b.py
index 3333333..4444444 100644
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-old_b = 0
+new_b = 1
"""

TWO_FINDING_DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-old_a = 0
-old_b = 0
+new_a = 1
+new_b = 1
"""

DELETION_ONLY_DIFF = """diff --git a/guard.py b/guard.py
index 1111111..2222222 100644
--- a/guard.py
+++ b/guard.py
@@ -1,3 +1 @@
-if user is None:
-    return forbidden()
 handle(user)
"""

RENAMED_DELETION_DIFF = """diff --git a/old.py b/new.py
similarity index 50%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -1,2 +1 @@
-guard()
 keep()
"""

MIXED_LOCKFILE_DIFF = (
    TWO_FINDING_DIFF
    + """diff --git a/package-lock.json b/package-lock.json
index 3333333..4444444 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-old-lock
+new-lock
"""
)


def _pr() -> PRInfo:
    return PRInfo(
        url="https://github.example/acme/widgets/pull/7",
        owner="acme",
        repo="widgets",
        number=7,
        clone_url="https://github.example/acme/widgets.git",
        title="Exercise engine behavior",
        body="A fixture PR",
        base_ref="main",
        base_sha="1" * 40,
        head_ref="feature",
        head_sha="2" * 40,
        resolved_at="2026-08-21T00:00:00Z",
    )


def _fast_config(**changes: Any) -> ReviewConfig:
    values: dict[str, Any] = {
        "model": "openai/test-model",
        "profile": "fast",
        "verifier_model": "none",
        "max_chunk_chars": 256,
        "llm_concurrency": 2,
    }
    values.update(changes)
    return ReviewConfig(**values)


def _balanced_config(**changes: Any) -> ReviewConfig:
    values: dict[str, Any] = {
        "model": "openai/generator",
        "profile": "balanced",
        "verifier_model": "openai/verifier",
        "max_chunk_chars": 256,
        "llm_concurrency": 2,
    }
    values.update(changes)
    return ReviewConfig(**values)


def _finding_payload(
    path: str,
    line: int,
    evidence: str,
    *,
    title: str,
    confidence: float = 0.99,
) -> dict[str, Any]:
    return {
        "title": title,
        "path": path,
        "side": "RIGHT",
        "line": line,
        "end_line": line,
        "severity": "high",
        "category": "bug",
        "confidence": confidence,
        "evidence": evidence,
        "root_cause": "The changed assignment violates the required invariant.",
        "failure_mode": "The affected path returns an incorrect value.",
        "fix_scope": "local",
        "trigger": "The changed assignment is reached.",
        "impact": "The resulting value is incorrect.",
        "suggested_fix": "Restore the intended assignment.",
    }


def _call(stage: str, chunk_id: str | None, *, error: str | None = None) -> CallRecord:
    return CallRecord(
        stage=stage,
        gateway="fake",
        requested_model="openai/test-model",
        resolved_model="test-model",
        latency_ms=1,
        chunk_id=chunk_id,
        input_tokens=10,
        output_tokens=5,
        response_sha256="a" * 64 if error is None else None,
        error=error,
    )


def test_exact_generation_prompt_budget_clips_context_before_patch() -> None:
    batch = engine_module._GenerationBatch(
        batch_id="batch",
        chunks=(),
        patch="+" + "p" * 11_000,
        context="selected evidence\n" + "c" * 30_000,
    )

    prompt, fitted, clipped = engine_module._fit_generation_prompt(
        batch,
        max_input_chars=40_960,
        pr_title="\x01" * 500,
        pr_body="\x01" * 4_000,
        allowed_categories=("bug",),
        review_policy="codereviewbench",
    )

    assert clipped is True
    assert len(prompt) <= 40_960
    assert batch.patch in prompt
    assert fitted.context.startswith("selected evidence")
    assert "BUGBUNNY_TRUNCATED_GENERATION_CONTEXT" in fitted.context


def test_exact_verifier_prompt_budget_retains_patch_and_clips_context() -> None:
    finding = Finding(
        title="Defect",
        body="impact",
        path="app.py",
        line=1,
        end_line=1,
        severity="high",
        category="bug",
        confidence=0.9,
        evidence="broken()",
        trigger="input",
        impact="impact",
        suggested_fix="fix it",
        chunk_id="chunk",
    )
    patch = "R1 | +broken()\n" + "p" * 8_000
    context = "model-selected evidence\n" + "c" * 40_000

    prompt, fitted_context, clipped = engine_module._fit_verifier_prompt(
        [finding],
        patch,
        context,
        max_batch_size=20,
        max_input_chars=24_000,
    )

    assert clipped is True
    assert len(prompt) <= 24_000
    assert patch in prompt
    assert fitted_context.startswith("model-selected evidence")


class FakeSnapshot:
    def __init__(
        self,
        diff: str,
        sources: dict[str, str],
        base_sources: dict[str, str] | None = None,
    ) -> None:
        self.raw_diff = diff
        self.sources = sources
        # When base sources are supplied, base and head content genuinely
        # differ so a wrong-revision read fails instead of silently passing.
        self.base_sources = base_sources
        self.head_sha = "2" * 40
        self.assert_clean_calls = 0
        self.close_calls = 0

    def diff(self, context_lines: int) -> str:
        assert context_lines >= 0
        return self.raw_diff

    def read_text(self, path: str) -> str:
        return self.sources[path]

    def read_blob(self, revision: str, path: str, *, max_bytes: int = 8_000_000) -> str:
        assert max_bytes > 0
        if self.base_sources is not None:
            assert revision != self.head_sha, "base-revision read routed to head"
            return self.base_sources[path]
        return self.sources[path]

    def list_files(self, _revision: str) -> list[str]:
        return sorted(self.sources)

    def git_grep(self, pattern: str, **kwargs: Any) -> tuple[Any, ...]:
        paths = kwargs.get("paths")
        allowed = set(paths) if paths else set(self.sources)
        hits = []
        for path, source in sorted(self.sources.items()):
            if path not in allowed and not any(path.startswith(prefix + "/") for prefix in allowed):
                continue
            for line, text in enumerate(source.splitlines(), start=1):
                if pattern in text:
                    hits.append((path, line, text))
        return tuple(hits[: int(kwargs.get("limit", 20))])

    def assert_clean(self) -> None:
        self.assert_clean_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class FakeCache:
    def __init__(self, snapshot: FakeSnapshot) -> None:
        self.snapshot = snapshot
        self.requests: list[PRInfo] = []

    def acquire(self, pr: PRInfo) -> FakeSnapshot:
        self.requests.append(pr)
        return self.snapshot


class FakeContextBuilder:
    def __init__(self, snapshot: FakeSnapshot, config: ReviewConfig, *, pr: PRInfo) -> None:
        self.snapshot = snapshot
        self.config = config
        self.pr = pr

    def build(self, parsed: Any, plan: Any) -> SimpleNamespace:
        return SimpleNamespace(
            by_chunk={
                chunk.chunk_id: SimpleNamespace(prompt=f"context for {chunk.path}")
                for chunk in plan.chunks
            },
            stats={"packets": len(plan.chunks)},
        )


class FakeGateway:
    def __init__(
        self,
        *,
        generation: dict[str, dict[str, Any] | BaseException],
        verification: dict[str, Any] | BaseException | None = None,
        selection: list[dict[str, Any] | BaseException] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.generation = generation
        self.verification = verification
        self.selection = list(selection or [])
        self.delays = delays or {}
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    async def complete_json(self, prompt: str, **kwargs: Any) -> GatewayResult:
        self.calls.append({"prompt": prompt, **kwargs})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delays.get(str(kwargs.get("chunk_id")), 0))
            stage = kwargs["stage"]
            chunk_id = str(kwargs.get("chunk_id"))
            if stage == "generation":
                outcome = self.generation.get(chunk_id, self.generation.get("*"))
            elif stage == "context_selection":
                outcome = self.selection.pop(0) if self.selection else None
            else:
                outcome = self.verification
            if isinstance(outcome, BaseException):
                raise outcome
            assert outcome is not None
            return GatewayResult(payload=outcome, call=_call(stage, chunk_id))
        finally:
            self.active -= 1


def _patch_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "ContextBuilder", FakeContextBuilder)


def test_line_source_cap_expands_from_the_finding_anchor() -> None:
    source = "\n".join(
        "TARGET_ANCHOR" if number == 50 else f"line-{number}-" + "x" * 80
        for number in range(1, 101)
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": source})
    finding = Finding.from_dict(
        _finding_payload("app.py", 50, "TARGET_ANCHOR", title="Anchored"),
        chunk_id="chunk",
    )

    excerpt = engine_module._line_source(
        snapshot,
        finding,
        base_sha="1" * 40,
        max_chars=100,
    )

    assert len(excerpt) <= 100
    assert "    50 | TARGET_ANCHOR" in excerpt
    assert "    32 |" not in excerpt


def test_verifier_evidence_fairly_preserves_sources_and_generation_contexts() -> None:
    plan = parse_unified_diff(TWO_FILE_DIFF).chunk(512)
    first, second = plan.chunks
    findings = [
        Finding.from_dict(
            _finding_payload("a.py", 50, "SOURCE_A_ANCHOR", title="First"),
            chunk_id=first.chunk_id,
        ),
        Finding.from_dict(
            _finding_payload("b.py", 50, "SOURCE_B_ANCHOR", title="Second"),
            chunk_id=second.chunk_id,
        ),
    ]

    def long_source(anchor: str) -> str:
        return "\n".join(
            anchor + "-" + "z" * 100 if number == 50 else f"line-{number}-" + "z" * 100
            for number in range(1, 101)
        )

    snapshot = FakeSnapshot(
        TWO_FILE_DIFF,
        {
            "a.py": long_source("SOURCE_A_ANCHOR"),
            "b.py": long_source("SOURCE_B_ANCHOR"),
        },
    )
    patch, context, metrics = engine_module._verification_evidence(
        findings,
        {chunk.chunk_id: chunk for chunk in plan.chunks},
        {
            first.chunk_id: "CTX_A_ANCHOR\n" + "a" * 2_000,
            second.chunk_id: "CTX_B_ANCHOR\n" + "b" * 2_000,
        },
        snapshot,
        max_context_chars=600,
        max_patch_chars=300,
        base_sha="1" * 40,
        base_paths={},
    )

    assert len(patch) <= 300
    assert "### candidate 0" in patch
    assert "### candidate 1" in patch
    assert len(context) <= 600
    assert "CTX_A_ANCHOR" in context
    assert "CTX_B_ANCHOR" in context
    assert "    50 | SOURCE_A_ANCHOR" in context
    assert "    50 | SOURCE_B_ANCHOR" in context
    assert context.index("### generation context") < context.index("### RIGHT source")
    assert metrics["patch_budget_clipped"] is True
    assert metrics["context_budget_clipped"] is True
    assert metrics["evidence_budget_clipped"] is True
    assert metrics["context_chars_omitted_by_evidence_budget"] > 0


def test_context_file_detection_uses_structured_evidence_not_path_substrings() -> None:
    context = "UNTRUSTED IMMUTABLE HEAD FILE tests/a.py L1-L1\n      1 | value = 1"

    assert engine_module._context_exposes_path(context, "tests/a.py") is True
    assert engine_module._context_exposes_path(context, "a.py") is False


def test_verifier_file_metrics_keep_generation_and_source_classes_separate() -> None:
    context = (
        "### generation context: chunk\n"
        "UNTRUSTED IMMUTABLE HEAD FILE same.py L1-L1\n"
        "      1 | generated = True\n\n"
        "### RIGHT source: other.py:7\n"
        "     7 | source = True"
    )

    generation = engine_module._verifier_generation_context(context)
    assert engine_module._context_exposes_path(generation, "same.py") is True
    assert engine_module._context_exposes_path(generation, "other.py") is False
    assert engine_module._verifier_source_exposes_path(context, "same.py") is False
    assert engine_module._verifier_source_exposes_path(context, "other.py") is True


@pytest.mark.asyncio
async def test_small_multifile_diff_is_packed_into_one_generation_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(max_chunk_chars=512)
    snapshot = FakeSnapshot(
        TWO_FILE_DIFF,
        {"a.py": "new_a = 1\n", "b.py": "new_b = 1\n"},
    )
    gateway = FakeGateway(generation={"*": {"findings": []}})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.diff["chunks"] == 2
    assert artifact.diff["generation_batches"] == 1
    assert len([call for call in gateway.calls if call["stage"] == "generation"]) == 1


@pytest.mark.asyncio
async def test_successful_multichunk_review_has_deterministic_order_and_full_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    plan = parse_unified_diff(TWO_FILE_DIFF).chunk(config.max_chunk_chars)
    assert len(plan.chunks) == 2
    first, second = plan.chunks
    gateway = FakeGateway(
        generation={
            first.chunk_id: {
                "findings": [_finding_payload("a.py", 1, "new_a = 1", title="A is wrong")]
            },
            second.chunk_id: {
                "findings": [_finding_payload("b.py", 1, "new_b = 1", title="B is wrong")]
            },
        },
        # Force completion in reverse order; the artifact must retain chunk order.
        delays={first.chunk_id: 0.03, second.chunk_id: 0.001},
    )
    snapshot = FakeSnapshot(
        TWO_FILE_DIFF,
        {"a.py": "new_a = 1\n", "b.py": "new_b = 1\n"},
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.complete is True
    assert artifact.coverage.completed_hunks == [
        hunk.hunk_id for hunk in parse_unified_diff(TWO_FILE_DIFF).hunks
    ]
    assert artifact.coverage.failed_hunks == []
    assert [finding.path for finding in artifact.raw_findings] == ["a.py", "b.py"]
    assert [finding.path for finding in artifact.findings] == ["a.py", "b.py"]
    assert [call.chunk_id for call in artifact.calls] == [
        first.chunk_id,
        second.chunk_id,
    ]
    assert gateway.max_active == 2
    assert artifact.diff["chunk_plan_complete"] is True
    assert snapshot.assert_clean_calls == 1
    assert snapshot.close_calls == 1


@pytest.mark.asyncio
async def test_one_generation_failure_marks_partial_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    plan = parse_unified_diff(TWO_FILE_DIFF).chunk(config.max_chunk_chars)
    first, second = plan.chunks
    failure_call = _call("generation", second.chunk_id, error="temporary failure")
    gateway = FakeGateway(
        generation={
            first.chunk_id: {
                "findings": [_finding_payload("a.py", 1, "new_a = 1", title="A is wrong")]
            },
            second.chunk_id: GatewayError("temporary failure", failure_call),
        }
    )
    snapshot = FakeSnapshot(
        TWO_FILE_DIFF,
        {"a.py": "new_a = 1\n", "b.py": "new_b = 1\n"},
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "partial"
    assert artifact.coverage.complete is False
    assert artifact.coverage.completed_hunks == [plan.chunks[0].hunk_ids[0]]
    assert artifact.coverage.failed_hunks == [plan.chunks[1].hunk_ids[0]]
    assert [finding.path for finding in artifact.findings] == ["a.py"]
    assert artifact.calls[-1].error == "temporary failure"
    assert artifact.diagnostics[0]["stage"] == "generation"
    assert snapshot.close_calls == 1


@pytest.mark.asyncio
async def test_objective_validation_rejection_is_audited_without_losing_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload(
                        "app.py",
                        99,
                        "invented evidence",
                        title="Invalid location",
                    )
                ]
            }
        }
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.complete is True
    assert len(artifact.raw_findings) == 1
    assert artifact.findings == []
    assert len(artifact.rejected_findings) == 1
    rejection = artifact.rejected_findings[0]
    assert rejection.stage == "validation"
    assert rejection.reason == "line is not an added-side changed line"


@pytest.mark.asyncio
async def test_malformed_generation_sibling_is_quarantined_without_losing_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    valid = _finding_payload("app.py", 1, "new_a = 1", title="Valid finding")
    malformed = dict(valid, title="Malformed finding", category="unknown-domain")
    gateway = FakeGateway(
        generation={chunk.chunk_id: {"findings": [valid, malformed]}}
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.complete is True
    assert [finding.title for finding in artifact.raw_findings] == ["Valid finding"]
    diagnostic = next(
        item
        for item in artifact.diagnostics
        if item.get("code") == "invalid_findings_quarantined"
    )
    assert diagnostic["stage"] == "generation_payload"
    assert diagnostic["count"] == "1"


@pytest.mark.asyncio
async def test_excluded_file_cannot_reenter_through_a_hallucinated_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(max_chunk_chars=512)
    gateway = FakeGateway(
        generation={
            "*": {
                "findings": [
                    _finding_payload(
                        "package-lock.json",
                        1,
                        "new-lock",
                        title="Excluded lockfile finding",
                    )
                ]
            }
        }
    )
    snapshot = FakeSnapshot(
        MIXED_LOCKFILE_DIFF,
        {"app.py": "new_a = 1\nnew_b = 1\n", "package-lock.json": "new-lock\n"},
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.excluded_files[0]["path"] == "package-lock.json"
    assert not artifact.findings
    assert artifact.rejected_findings[0].reason == "path is not a changed text file"


@pytest.mark.asyncio
async def test_deletion_only_defect_is_grounded_on_the_left_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    payload = _finding_payload(
        "guard.py",
        1,
        "if user is None:",
        title="Null guard removed",
    )
    payload["side"] = "LEFT"
    payload["end_line"] = 1
    snapshot = FakeSnapshot(
        DELETION_ONLY_DIFF,
        {"guard.py": "handle(user)\n"},
        base_sources={"guard.py": "if user is None:\n    return forbidden()\nhandle(user)\n"},
    )
    artifact = await ReviewEngine(
        config,
        FakeGateway(generation={"*": {"findings": [payload]}}),
        FakeCache(snapshot),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "completed"
    assert artifact.coverage.complete
    assert len(artifact.findings) == 1
    assert artifact.findings[0].side == "LEFT"


@pytest.mark.asyncio
async def test_renamed_file_left_finding_accepts_old_path_then_normalizes_to_review_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    payload = _finding_payload("old.py", 1, "guard()", title="Guard removed during rename")
    payload["side"] = "LEFT"
    snapshot = FakeSnapshot(
        RENAMED_DELETION_DIFF,
        {"new.py": "keep()\n"},
        base_sources={"old.py": "guard()\nkeep()\n"},
    )

    artifact = await ReviewEngine(
        _fast_config(),
        FakeGateway(generation={"*": {"findings": [payload]}}),
        FakeCache(snapshot),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "completed"
    assert len(artifact.findings) == 1
    assert artifact.findings[0].path == "new.py"
    assert artifact.findings[0].side == "LEFT"
    assert artifact.diff["commentable_ranges"]["LEFT"] == {"new.py": [[1, 1]]}


@pytest.mark.asyncio
async def test_verifier_keeps_and_drops_individual_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config()
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="Keep me"),
                    _finding_payload("app.py", 2, "new_b = 1", title="Drop me"),
                ]
            }
        },
        verification={
            "decisions": [
                {
                    "candidate_index": 0,
                    "decision": "keep",
                    "confidence": 0.96,
                    "reason": "The changed assignment demonstrably causes the issue.",
                    "canonical_index": None,
                    "family_key": "assignment_invariant",
                },
                {
                    "candidate_index": 1,
                    "decision": "drop",
                    "confidence": 0.93,
                    "reason": "The stated behavior does not follow from the patch.",
                    "canonical_index": None,
                    "family_key": "assignment_invariant",
                },
            ]
        },
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert [finding.title for finding in artifact.findings] == ["Keep me"]
    assert artifact.findings[0].verifier_confidence == pytest.approx(0.96)
    assert [item.finding.title for item in artifact.rejected_findings] == ["Drop me"]
    assert artifact.rejected_findings[0].stage == "verifier"
    assert [call["stage"] for call in gateway.calls] == ["generation", "verification"]


@pytest.mark.asyncio
async def test_agentic_context_is_selected_then_reused_by_generation_and_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(
        context_mode="agentic",
        context_selection_rounds=1,
        max_context_chars=4_000,
        initial_context_chars=1_000,
        max_context_files=4,
        context_read_lines=40,
        context_read_chars=1_000,
        repository_index_chars=1_000,
    )
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        selection=[
            {
                "requests": [
                    {
                        "action": "read",
                        "path": "deps.py",
                        "query": "",
                        "start_line": 1,
                        "end_line": 2,
                    }
                ],
                "done": True,
            }
        ],
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="Context-backed defect")
                ]
            }
        },
        verification={
            "decisions": [
                {
                    "candidate_index": 0,
                    "decision": "keep",
                    "confidence": 0.99,
                    "reason": "The dependency contract confirms the changed value is invalid.",
                    "canonical_index": None,
                    "family_key": "dependency_contract",
                }
            ]
        },
    )
    snapshot = FakeSnapshot(
        TWO_FINDING_DIFF,
        {
            "app.py": "new_a = 1\nnew_b = 1\n",
            "deps.py": "EXPECTED_A = 0\nEXPECTED_B = 0\n",
            "x" * 4_097: "unreachable inventory path\n",
        },
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert [call.stage for call in artifact.calls] == [
        "context_selection",
        "generation",
        "verification",
    ]
    generation_prompt = next(
        call["prompt"] for call in gateway.calls if call["stage"] == "generation"
    )
    verifier_prompt = next(
        call["prompt"] for call in gateway.calls if call["stage"] == "verification"
    )
    assert "EXPECTED_A = 0" in generation_prompt
    assert "EXPECTED_A = 0" in verifier_prompt
    assert artifact.context["mode"] == "agentic"
    assert artifact.context["effective_context_files_exposed_to_model"] == ["deps.py"]
    assert artifact.context["unique_unchanged_context_files_exposed_to_model"] == 1
    assert artifact.context["selection"]["failed_batches"] == []
    assert artifact.context["context_pressure"]["selector_inventory_files_omitted"] == 1
    assert (
        artifact.context["context_pressure"]["selection_bound_hits"][
            "repository_inventory_omission_hit"
        ]
        == 1
    )
    assert artifact.context["context_pressure"]["selection_batches_hitting_any_bound"] == 1
    assert gateway.calls[0]["max_output_tokens"] == 16_384
    assert gateway.calls[1]["max_output_tokens"] == config.max_output_tokens
    assert gateway.calls[2]["max_output_tokens"] == config.max_output_tokens


@pytest.mark.asyncio
async def test_repository_inventory_omissions_are_not_multiplied_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(context_mode="agentic", context_selection_rounds=1)
    chunks = parse_unified_diff(TWO_FILE_DIFF).chunk(config.max_chunk_chars).chunks
    gateway = FakeGateway(
        selection=[{"requests": [], "done": True} for _chunk in chunks],
        generation={chunk.chunk_id: {"findings": []} for chunk in chunks},
    )
    snapshot = FakeSnapshot(
        TWO_FILE_DIFF,
        {
            "a.py": "new_a = 1\n",
            "b.py": "new_b = 1\n",
            "x" * 4_097: "unreachable inventory path\n",
        },
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert len(artifact.context["selection"]["batches"]) == len(chunks)
    pressure = artifact.context["context_pressure"]
    assert pressure["selector_inventory_files_omitted"] == 1
    assert pressure["selection_batches_with_inventory_omissions"] == len(chunks)


@pytest.mark.asyncio
async def test_selector_observation_clipping_is_aggregated_as_context_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(
        context_mode="agentic",
        context_selection_rounds=1,
        context_read_chars=70,
    )
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        selection=[
            {
                "requests": [
                    {
                        "action": "list",
                        "path": "",
                        "query": "",
                        "start_line": None,
                        "end_line": None,
                    }
                ],
                "done": True,
            }
        ],
        generation={chunk.chunk_id: {"findings": []}},
    )
    snapshot = FakeSnapshot(
        TWO_FINDING_DIFF,
        {
            "app.py": "new_a = 1\nnew_b = 1\n",
            "another_module_with_a_long_name.py": "value = 1\n",
        },
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    pressure = artifact.context["context_pressure"]
    assert pressure["selection_bound_hits"]["selector_observations_truncated"] == 1
    assert pressure["selection_batches_with_selector_observation_clipping"] == 1
    assert pressure["selection_batches_hitting_any_bound"] == 1


@pytest.mark.asyncio
async def test_agentic_selector_failure_marks_coverage_failed_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(
        context_mode="agentic",
        context_selection_rounds=1,
        max_context_chars=4_000,
        initial_context_chars=1_000,
    )
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    failure_call = _call("context_selection", chunk.chunk_id, error="selector unavailable")
    gateway = FakeGateway(
        selection=[GatewayError("selector unavailable", failure_call)],
        generation={},
    )
    artifact = await ReviewEngine(
        config,
        gateway,
        FakeCache(FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "failed"
    assert artifact.coverage.completed_hunks == []
    assert artifact.coverage.failed_hunks == [chunk.hunk_ids[0]]
    assert [call.stage for call in artifact.calls] == ["context_selection"]
    assert not any(call["stage"] == "generation" for call in gateway.calls)
    assert artifact.context["selection"]["failed_batches"] == [chunk.chunk_id]


@pytest.mark.asyncio
async def test_optional_agentic_search_failure_keeps_seed_and_reviews_full_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config(
        context_mode="agentic",
        context_selection_rounds=1,
        max_context_chars=4_000,
        initial_context_chars=1_000,
    )

    class SearchFailingSnapshot(FakeSnapshot):
        def git_grep(self, pattern: str, **kwargs: Any) -> tuple[Any, ...]:
            raise RuntimeError("optional search unavailable")

    gateway = FakeGateway(
        selection=[
            {
                "requests": [
                    {
                        "action": "search",
                        "path": "",
                        "query": "new_a",
                        "start_line": None,
                        "end_line": None,
                    }
                ],
                "done": True,
            }
        ],
        generation={"*": {"findings": []}},
    )
    snapshot = SearchFailingSnapshot(
        TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"}
    )

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.complete is True
    assert [call["stage"] for call in gateway.calls] == ["context_selection", "generation"]
    assert any(item.get("code") == "search_failed" for item in artifact.diagnostics)


@pytest.mark.asyncio
async def test_verifier_batches_are_bounded_by_serialized_candidate_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(
        verification_batch_chars=900,
        verifier_context_window_tokens=64_000,
        verifier_input_char_budget=54_272,
    )
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="First"),
                    _finding_payload("app.py", 2, "new_b = 1", title="Second"),
                ]
            }
        },
        verification={
            "decisions": [
                {
                    "candidate_index": 0,
                    "decision": "keep",
                    "confidence": 0.99,
                    "reason": "grounded",
                    "canonical_index": None,
                    "family_key": "assignment_invariant",
                }
            ]
        },
    )
    artifact = await ReviewEngine(
        config,
        gateway,
        FakeCache(FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "completed"
    assert len(artifact.findings) == 2
    assert [call["stage"] for call in gateway.calls] == [
        "generation",
        "verification",
        "verification",
    ]
    assert all(
        metrics["candidate_payload_chars"] <= config.verification_batch_chars
        for metrics in artifact.context["verification_batches"]
    )
    budget = artifact.context["budget"]
    assert budget["source"] == "fixed"
    assert budget["verifier_source"] == "declared_window"
    assert budget["declared_window_reserve_tokens"] is None
    assert budget["declared_window_input_character_assumption"] is None
    assert budget["declared_verifier_window_reserve_tokens"] == 4_096
    assert "not a tokenizer" in budget["declared_verifier_window_input_character_assumption"]
    assert artifact.runtime == {
        "generation": {
            "requested_model": "openai/generator",
            "requested_reasoning_effort": "low",
            "reasoning_effort_parameter_will_be_sent": None,
            "transport": "FakeGateway",
        },
        "verification": {
            "requested_model": "openai/verifier",
            "requested_reasoning_effort": "low",
            "reasoning_effort_parameter_will_be_sent": None,
            "transport": "FakeGateway",
        },
    }


def test_verifier_batch_char_bound_uses_exact_wrapped_payload_and_rejects_singleton() -> None:
    first = Finding.from_dict(
        _finding_payload("app.py", 1, "new_a = 1", title="First"),
        chunk_id="chunk",
    )
    second = Finding.from_dict(
        _finding_payload("app.py", 2, "new_b = 1", title="Second"),
        chunk_id="chunk",
    )
    one_payload_chars = max(
        len(engine_module.verifier_candidate_payload([first])),
        len(engine_module.verifier_candidate_payload([second])),
    )

    batches = engine_module._verification_batches(
        [first, second],
        max_items=20,
        max_chars=one_payload_chars,
    )

    assert [offset for offset, _batch in batches] == [0, 1]
    assert all(
        len(engine_module.verifier_candidate_payload(batch)) <= one_payload_chars
        for _offset, batch in batches
    )
    with pytest.raises(engine_module.ReviewEngineError, match="verification_batch_chars"):
        engine_module._verification_batches(
            [first],
            max_items=20,
            max_chars=len(engine_module.verifier_candidate_payload([first])) - 1,
        )


def test_verifier_batches_split_before_rendering_capacity_plus_one() -> None:
    finding = Finding.from_dict(
        _finding_payload("app.py", 1, "new_a = 1", title="Candidate"),
        chunk_id="chunk",
    )

    batches = engine_module._verification_batches(
        [finding] * 21,
        max_items=20,
        max_chars=1_000_000,
        max_prompt_chars=1_000_000,
    )

    assert [(offset, len(batch)) for offset, batch in batches] == [(0, 20), (20, 1)]


@pytest.mark.asyncio
async def test_twenty_one_candidates_are_verified_in_two_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(verification_batch_size=20)
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    candidates = [
        _finding_payload(
            "app.py",
            1 if index % 2 == 0 else 2,
            "new_a = 1" if index % 2 == 0 else "new_b = 1",
            title=f"Candidate {index}",
        )
        for index in range(21)
    ]

    class BatchAwareGateway(FakeGateway):
        verification_calls = 0

        async def complete_json(self, prompt: str, **kwargs: Any) -> GatewayResult:
            if kwargs["stage"] == "generation":
                return await super().complete_json(prompt, **kwargs)
            count = 20 if self.verification_calls == 0 else 1
            self.verification_calls += 1
            return GatewayResult(
                payload={
                    "decisions": [
                        {
                            "candidate_index": index,
                            "decision": "keep",
                            "confidence": 0.99,
                            "reason": "The changed assignment is present.",
                            "canonical_index": None,
                            "family_key": f"assignment_{self.verification_calls}_{index}",
                        }
                        for index in range(count)
                    ]
                },
                call=_call("verification", str(kwargs.get("chunk_id"))),
            )

    gateway = BatchAwareGateway(generation={chunk.chunk_id: {"findings": candidates}})
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert artifact.coverage.complete is True
    assert len(artifact.validated_findings) == 21
    assert gateway.verification_calls == 2


@pytest.mark.asyncio
async def test_semantically_invalid_verifier_response_is_retried_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(verification_semantic_retries=2)
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]

    class RepairingGateway(FakeGateway):
        verification_calls = 0

        async def complete_json(self, prompt: str, **kwargs: Any) -> GatewayResult:
            if kwargs["stage"] == "generation":
                return await super().complete_json(prompt, **kwargs)
            self.verification_calls += 1
            invalid = self.verification_calls == 1
            return GatewayResult(
                payload={
                    "decisions": [
                        {
                            "candidate_index": 0,
                            "decision": "merge" if invalid else "keep",
                            "confidence": 0.99,
                            "reason": "The assignment is present.",
                            "canonical_index": 0 if invalid else None,
                            "family_key": "assignment_invariant",
                        }
                    ]
                },
                call=_call("verification", str(kwargs.get("chunk_id"))),
            )

    gateway = RepairingGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="Valid candidate")
                ]
            }
        }
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "completed"
    assert gateway.verification_calls == 2
    verification_calls = [call for call in artifact.calls if call.stage == "verification"]
    assert len(verification_calls) == 2
    assert verification_calls[0].error is not None
    assert verification_calls[1].error is None
    retry = next(
        item
        for item in artifact.diagnostics
        if item.get("stage") == "verification_semantic_retry"
    )
    assert retry["retry_count"] == 1
    assert retry["recovered"] is True


@pytest.mark.asyncio
async def test_exhausted_semantic_verifier_retries_remain_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(verification_semantic_retries=1)
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    invalid_decision = {
        "decisions": [
            {
                "candidate_index": 0,
                "decision": "merge",
                "confidence": 0.99,
                "reason": "Invalid self-merge.",
                "canonical_index": 0,
                "family_key": "assignment_invariant",
            }
        ]
    }
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="Candidate")
                ]
            }
        },
        verification=invalid_decision,
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "failed"
    assert artifact.findings == []
    verification_calls = [call for call in artifact.calls if call.stage == "verification"]
    assert len(verification_calls) == 2
    assert all(call.error is not None for call in verification_calls)
    failure = next(
        item for item in artifact.diagnostics if item.get("stage") == "verification"
    )
    assert failure["semantic_attempt_count"] == 2
    assert failure["semantic_retry_count"] == 1
    assert failure["failure_policy"] == "fail_closed"


def test_runtime_provenance_records_requested_and_parameter_send_policy() -> None:
    gateway = ModelGateway(GatewayConfig(api_key="test-key"))
    runtime = engine_module.review_runtime_provenance(
        gateway,
        model="anthropic/claude-opus-4-5",
        verifier_model="openai/gpt-5.6-terra",
        generation_reasoning_effort="high",
        verifier_reasoning_effort="medium",
    )

    assert runtime["generation"]["requested_reasoning_effort"] == "high"
    assert runtime["generation"]["reasoning_effort_parameter_will_be_sent"] is False
    assert runtime["verification"]["requested_reasoning_effort"] == "medium"
    assert runtime["verification"]["reasoning_effort_parameter_will_be_sent"] is True


@pytest.mark.asyncio
async def test_verifier_failure_is_fail_closed_and_preserves_audit_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config()
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    failure_call = _call("verification", "candidates-0-0", error="verifier unavailable")
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [_finding_payload("app.py", 1, "new_a = 1", title="Unverified")]
            }
        },
        verification=GatewayError("verifier unavailable", failure_call),
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})

    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]

    assert artifact.status == "failed"
    assert artifact.coverage.complete is True
    assert [finding.title for finding in artifact.raw_findings] == ["Unverified"]
    assert artifact.findings == []
    assert artifact.rejected_findings[0].stage == "verifier_error"
    assert artifact.diagnostics[-1]["failure_policy"] == "fail_closed"
    assert [call.stage for call in artifact.calls] == ["generation", "verification"]


@pytest.mark.asyncio
async def test_late_verifier_failure_accounts_for_candidates_kept_earlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_context(monkeypatch)
    config = _balanced_config(verification_batch_size=1)
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]

    class SequencedGateway(FakeGateway):
        verification_calls = 0

        async def complete_json(self, prompt: str, **kwargs: Any) -> GatewayResult:
            if kwargs["stage"] == "generation":
                return await super().complete_json(prompt, **kwargs)
            self.verification_calls += 1
            if self.verification_calls == 1:
                return GatewayResult(
                    payload={
                        "decisions": [
                            {
                                "candidate_index": 0,
                                "decision": "keep",
                                "confidence": 0.99,
                                "reason": "grounded",
                                "canonical_index": None,
                                "family_key": "assignment_invariant",
                            }
                        ]
                    },
                    call=_call("verification", str(kwargs.get("chunk_id"))),
                )
            failure = _call(
                "verification", str(kwargs.get("chunk_id")), error="verifier unavailable"
            )
            raise GatewayError("verifier unavailable", failure)

    gateway = SequencedGateway(
        generation={
            chunk.chunk_id: {
                "findings": [
                    _finding_payload("app.py", 1, "new_a = 1", title="First"),
                    _finding_payload("app.py", 2, "new_b = 1", title="Second"),
                ]
            }
        }
    )
    artifact = await ReviewEngine(
        config,
        gateway,
        FakeCache(FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "failed"
    assert not artifact.findings
    assert {item.stage for item in artifact.rejected_findings} == {
        "verifier_error",
        "verifier_run_failed",
    }


@pytest.mark.asyncio
async def test_context_failure_marks_every_unattempted_hunk_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenContextBuilder(FakeContextBuilder):
        def build(self, parsed: Any, plan: Any) -> SimpleNamespace:
            raise RuntimeError("context unavailable")

    monkeypatch.setattr(engine_module, "ContextBuilder", BrokenContextBuilder)
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})
    artifact = await ReviewEngine(
        _fast_config(),
        FakeGateway(generation={}),
        FakeCache(snapshot),  # type: ignore[arg-type]
    ).review(_pr())

    assert artifact.status == "failed"
    assert artifact.coverage.completed_hunks == []
    assert len(artifact.coverage.failed_hunks) == artifact.coverage.eligible_hunks == 1


@pytest.mark.asyncio
async def test_artifact_writer_persists_native_json_and_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_context(monkeypatch)
    config = _fast_config()
    chunk = parse_unified_diff(TWO_FINDING_DIFF).chunk(config.max_chunk_chars).chunks[0]
    gateway = FakeGateway(
        generation={
            chunk.chunk_id: {
                "findings": [_finding_payload("app.py", 1, "new_a = 1", title="Persisted finding")]
            }
        }
    )
    snapshot = FakeSnapshot(TWO_FINDING_DIFF, {"app.py": "new_a = 1\nnew_b = 1\n"})
    artifact = await ReviewEngine(config, gateway, FakeCache(snapshot)).review(_pr())  # type: ignore[arg-type]
    json_path = tmp_path / "nested" / "review.json"
    markdown_path = tmp_path / "nested" / "review.md"

    returned_json, returned_markdown = write_review_artifact(
        artifact,
        json_path,
        markdown_path,
    )

    assert returned_json == json_path.resolve()
    assert returned_markdown == markdown_path.resolve()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "bugbunny-review-v2"
    assert payload["status"] == "completed"
    assert payload["findings"][0]["title"] == "Persisted finding"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# BugBunny review\n")
    assert "Persisted finding" in markdown
    assert "Coverage: 1/1 eligible hunks" in markdown


def test_anchor_patch_excerpt_matches_exact_coordinates_not_substrings() -> None:
    diff = (
        "diff --git a/R2D2.py b/R2D2.py\n"
        "--- a/R2D2.py\n"
        "+++ b/R2D2.py\n"
        "@@ -1,0 +2,1 @@\n"
        "+new = 1\n"
    )
    chunk = parse_unified_diff(diff).chunk(4_096).chunks[0]

    excerpt = engine_module._anchor_patch_excerpt(chunk, 2, "RIGHT", radius=0, max_chars=None)

    # The excerpt must center on the R2 payload row, not the file header whose
    # path merely contains the substring "R2".
    assert "+new = 1" in excerpt
    assert "diff --git" not in excerpt
