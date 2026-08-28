from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from bugbunny.util import is_finite_number

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal[
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
]
ReviewProfile = Literal["fast", "balanced"]
ReviewPolicyName = Literal["production", "codereviewbench"]
ContextMode = Literal["curated", "agentic"]
ReviewSide = Literal["RIGHT", "LEFT"]

DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS = 4_096
DECLARED_WINDOW_CHARS_PER_TOKEN = 2
# Length of exploration.INDEX_TRUNCATION_MARKER, the smallest repository index
# the agentic renderer can produce while disclosing truncation. A test pins
# the two values together (models must stay import-leaf, so no direct import).
MIN_REPOSITORY_INDEX_CHARS = 82
DECLARED_GENERATION_FRAMING_CHARS = 12_000


@dataclass(frozen=True)
class PRInfo:
    """Immutable pull-request identity used by the review engine."""

    url: str
    owner: str
    repo: str
    number: int
    clone_url: str
    title: str
    body: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    resolved_at: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PRInfo:
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ReviewConfig:
    """Public, secret-free configuration frozen into each run artifact."""

    model: str = "openai/gpt-5.6-luna"
    verifier_model: str | None = "same"
    profile: ReviewProfile = "balanced"
    review_policy: ReviewPolicyName = "production"
    review_policy_version: str = "bugbunny-production-policy-v1"
    review_policy_sha256: str = ""
    reasoning_effort: str = "low"
    verifier_reasoning_effort: str = "low"
    context_mode: ContextMode = "curated"
    context_budget_source: Literal["fixed", "declared_window"] = "fixed"
    context_window_tokens: int | None = None
    generation_input_char_budget: int | None = None
    verifier_context_window_tokens: int | None = None
    verifier_input_char_budget: int | None = None
    diff_context_lines: int = 12
    max_chunk_chars: int = 72_000
    max_context_chars: int = 120_000
    initial_context_chars: int = 36_000
    source_context_lines: int = 100
    max_symbols_per_chunk: int = 24
    max_hits_per_symbol: int = 10
    context_selection_rounds: int = 2
    context_requests_per_round: int = 8
    max_context_files: int = 16
    context_read_lines: int = 240
    context_read_chars: int = 24_000
    context_blob_read_bytes: int = 16_000_000
    context_search_hits: int = 12
    context_search_max_offset: int = 100_000
    repository_index_chars: int = 60_000
    llm_concurrency: int = 4
    verification_batch_size: int = 20
    verification_batch_chars: int = 48_000
    verification_semantic_retries: int = 2
    timeout_seconds: int = 300
    max_output_tokens: int = 32_768
    verifier_max_output_tokens: int = 32_768
    min_verifier_confidence: float = 0.78
    operating_point_id: str | None = None
    operating_point_sha256: str | None = None
    include_categories: tuple[str, ...] = (
        "bug",
        "security",
        "concurrency",
        "data",
        "api",
        "performance",
        "test_gap",
        "doc_defect",
    )

    def validate(self) -> None:
        if self.profile not in {"fast", "balanced"}:
            raise ValueError("profile must be 'fast' or 'balanced'")
        from bugbunny.policy import get_review_policy

        policy = get_review_policy(self.review_policy)
        if self.review_policy_version != policy.version:
            raise ValueError("review_policy_version does not match the selected policy")
        if self.review_policy_sha256 and self.review_policy_sha256 != policy.sha256:
            raise ValueError("review_policy_sha256 does not match the selected policy")
        if not set(self.include_categories) <= set(policy.categories):
            raise ValueError("include_categories exceed the selected review policy")
        if self.context_mode not in {"curated", "agentic"}:
            raise ValueError("context_mode must be 'curated' or 'agentic'")
        if self.context_budget_source not in {"fixed", "declared_window"}:
            raise ValueError("context_budget_source must be 'fixed' or 'declared_window'")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.profile == "fast" and self.verifier_model not in {None, "none"}:
            raise ValueError("fast profile requires verifier_model='none'")
        if not 0 <= self.min_verifier_confidence <= 1:
            raise ValueError("min_verifier_confidence must be between 0 and 1")
        if self.context_window_tokens is not None and self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive when provided")
        if (
            self.verifier_context_window_tokens is not None
            and self.verifier_context_window_tokens <= 0
        ):
            raise ValueError("verifier_context_window_tokens must be positive when provided")
        if self.context_budget_source == "declared_window" and self.context_window_tokens is None:
            raise ValueError("declared_window context budgets require context_window_tokens")
        if (
            self.context_budget_source == "declared_window"
            and self.generation_input_char_budget is None
        ):
            raise ValueError("declared_window context budgets require generation_input_char_budget")
        if self.context_budget_source == "fixed" and (
            self.context_window_tokens is not None or self.generation_input_char_budget is not None
        ):
            raise ValueError("fixed context budgets cannot declare a generation window")
        if (self.verifier_context_window_tokens is None) != (
            self.verifier_input_char_budget is None
        ):
            raise ValueError(
                "verifier_context_window_tokens and verifier_input_char_budget must be set together"
            )
        for name in (
            "diff_context_lines",
            "max_chunk_chars",
            "max_context_chars",
            "initial_context_chars",
            "source_context_lines",
            "max_symbols_per_chunk",
            "max_hits_per_symbol",
            "context_selection_rounds",
            "context_requests_per_round",
            "max_context_files",
            "context_read_lines",
            "context_read_chars",
            "context_blob_read_bytes",
            "context_search_hits",
            "context_search_max_offset",
            "repository_index_chars",
            "llm_concurrency",
            "verification_batch_size",
            "verification_batch_chars",
            "timeout_seconds",
            "max_output_tokens",
            "verifier_max_output_tokens",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("generation_input_char_budget", "verifier_input_char_budget"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when provided")
        if not 0 <= self.verification_semantic_retries <= 5:
            raise ValueError("verification_semantic_retries must be between 0 and 5")
        if self.context_requests_per_round > 32:
            raise ValueError("context_requests_per_round cannot exceed 32")
        if self.context_selection_rounds > 8:
            raise ValueError("context_selection_rounds cannot exceed 8")
        if self.context_blob_read_bytes > 256_000_000:
            raise ValueError("context_blob_read_bytes cannot exceed 256000000")
        if self.context_search_max_offset > 1_000_000:
            raise ValueError("context_search_max_offset cannot exceed 1000000")
        if self.repository_index_chars < MIN_REPOSITORY_INDEX_CHARS:
            # Below the index renderer's truncation-marker length, every
            # agentic batch on a real repository fails before selection; a
            # validate()-accepted config must not be operationally unusable.
            raise ValueError(
                f"repository_index_chars must be at least {MIN_REPOSITORY_INDEX_CHARS}"
            )
        if self.context_window_tokens is not None:
            reserved_tokens = self.max_output_tokens + DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
            if reserved_tokens >= self.context_window_tokens:
                raise ValueError("declared context window leaves no room for model input")
            input_char_envelope = (
                self.context_window_tokens - reserved_tokens
            ) * DECLARED_WINDOW_CHARS_PER_TOKEN
            if self.generation_input_char_budget != input_char_envelope:
                raise ValueError(
                    "generation_input_char_budget does not match the declared-window formula"
                )
            evidence_envelope = input_char_envelope - DECLARED_GENERATION_FRAMING_CHARS
            if evidence_envelope <= 0:
                raise ValueError("declared context window leaves no room for review evidence")
            if self.max_chunk_chars + self.max_context_chars > evidence_envelope:
                raise ValueError(
                    "patch and context budgets exceed the declared generation prompt envelope"
                )
            if (
                self.verifier_model not in {None, "none"}
                and self.verifier_input_char_budget is None
            ):
                raise ValueError(
                    "declared-window balanced reviews require a verifier context-window declaration"
                )
        if self.verifier_context_window_tokens is not None:
            verifier_reserved_tokens = (
                self.verifier_max_output_tokens + DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
            )
            if verifier_reserved_tokens >= self.verifier_context_window_tokens:
                raise ValueError("declared verifier context window leaves no room for model input")
            verifier_envelope = (
                self.verifier_context_window_tokens - verifier_reserved_tokens
            ) * DECLARED_WINDOW_CHARS_PER_TOKEN
            if self.verifier_input_char_budget != verifier_envelope:
                raise ValueError(
                    "verifier_input_char_budget does not match the declared-window formula"
                )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["include_categories"] = list(self.include_categories)
        return value


@dataclass
class Finding:
    """One atomic defect anchored to a changed line on either diff side."""

    title: str
    body: str
    path: str
    line: int
    end_line: int
    severity: Severity
    category: Category
    confidence: float
    evidence: str
    trigger: str
    impact: str
    suggested_fix: str
    chunk_id: str
    root_cause: str = ""
    failure_mode: str = ""
    fix_scope: str = "local"
    side: ReviewSide = "RIGHT"
    finding_id: str = ""
    fingerprint: str = ""
    verifier_confidence: float | None = None
    verifier_reason: str | None = None
    verifier_family_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, chunk_id: str = "") -> Finding:
        return cls(
            title=str(value.get("title", "")).strip(),
            body=str(value.get("body", "")).strip(),
            # Git paths may legitimately end in spaces. Preserve the artifact
            # byte-for-byte here; safety and diff-membership checks belong to
            # deterministic validation rather than lossy model hydration.
            path=str(value.get("path", "")),
            line=int(value.get("line", 0) or 0),
            end_line=int(value.get("end_line", value.get("line", 0)) or 0),
            severity=str(value.get("severity", "medium")).lower(),  # type: ignore[arg-type]
            category=str(value.get("category", "bug")).lower(),  # type: ignore[arg-type]
            confidence=(
                float(value.get("confidence", 0.0) or 0.0)
                if is_finite_number(value.get("confidence", 0.0) or 0.0)
                else 0.0
            ),
            evidence=str(value.get("evidence", "")).strip(),
            trigger=str(value.get("trigger", "")).strip(),
            impact=str(value.get("impact", "")).strip(),
            suggested_fix=str(value.get("suggested_fix", "")).strip(),
            chunk_id=chunk_id or str(value.get("chunk_id", "")),
            root_cause=str(value.get("root_cause", "")).strip(),
            failure_mode=str(value.get("failure_mode", "")).strip(),
            fix_scope=str(value.get("fix_scope", "local")).strip().lower(),
            side=str(value.get("side", "RIGHT")).upper(),  # type: ignore[arg-type]
        )


@dataclass
class RejectedFinding:
    finding: Finding
    stage: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.to_dict(),
            "stage": self.stage,
            "reason": self.reason,
        }


@dataclass
class CallRecord:
    stage: str
    gateway: str
    requested_model: str
    resolved_model: str | None
    latency_ms: int
    chunk_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    response_sha256: str | None = None
    request_sha256: str | None = None
    schema_sha256: str | None = None
    error: str | None = None
    attempt_count: int = 1
    retry_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Coverage:
    total_files: int
    eligible_files: int
    excluded_files: list[dict[str, str]]
    total_hunks: int
    eligible_hunks: int
    completed_hunks: list[str]
    failed_hunks: list[str]
    eligible_hunk_ids: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        completed = set(self.completed_hunks)
        if self.eligible_hunk_ids:
            # Identity by exact hunk-ID set: a count match cannot prove that
            # the completed hunks are the eligible ones.
            return completed == set(self.eligible_hunk_ids) and not self.failed_hunks
        return self.eligible_hunks == len(completed) and not self.failed_hunks

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["complete"] = self.complete
        value["coverage_ratio"] = (
            len(set(self.completed_hunks)) / self.eligible_hunks if self.eligible_hunks else 1.0
        )
        return value


@dataclass
class ReviewArtifact:
    schema_version: str
    tool: str
    tool_version: str
    implementation: dict[str, Any]
    run_id: str
    status: Literal["completed", "partial", "failed"]
    started_at: str
    completed_at: str
    duration_ms: int
    pr: PRInfo
    config: ReviewConfig
    runtime: dict[str, Any]
    diff: dict[str, Any]
    coverage: Coverage
    context: dict[str, Any]
    calls: list[CallRecord] = field(default_factory=list)
    raw_findings: list[Finding] = field(default_factory=list)
    validated_findings: list[Finding] = field(default_factory=list)
    rejected_findings: list[RejectedFinding] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    benchmark: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "implementation": dict(self.implementation),
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "pr": self.pr.to_dict(),
            "config": self.config.to_dict(),
            "runtime": self.runtime,
            "diff": self.diff,
            "coverage": self.coverage.to_dict(),
            "context": self.context,
            "calls": [item.to_dict() for item in self.calls],
            "raw_findings": [item.to_dict() for item in self.raw_findings],
            "validated_findings": [item.to_dict() for item in self.validated_findings],
            "rejected_findings": [item.to_dict() for item in self.rejected_findings],
            "findings": [item.to_dict() for item in self.findings],
            "diagnostics": self.diagnostics,
            "benchmark": self.benchmark,
        }
