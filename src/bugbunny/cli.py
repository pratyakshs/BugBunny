"""Command-line boundary for BugBunny.

The CLI deliberately keeps credentials in transport objects and environment
variables.  They are never added to :class:`ReviewConfig`, run manifests, or
review artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from bugbunny.build import (
    BENCHMARK_PLAN_SCHEMA_VERSION,
    BENCHMARK_RUN_SCHEMA_VERSION,
    EXPORT_INDEX_SCHEMA_VERSION,
    EXPORT_MANIFEST_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    implementation_identity,
)
from bugbunny.gateway import MARTIAN_API_BASE, MARTIAN_API_KEY_ENV, GatewayConfig, ModelGateway
from bugbunny.models import (
    DECLARED_GENERATION_FRAMING_CHARS,
    DECLARED_WINDOW_CHARS_PER_TOKEN,
    DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS,
    PRInfo,
    ReviewConfig,
)
from bugbunny.util import atomic_write_json, file_lock, load_json, sha256_bytes, utc_now

DEFAULT_MODEL = "openai/gpt-5.6-luna"
DEFAULT_CACHE_DIR = Path(".bugbunny-cache")
DEFAULT_RUNS_DIR = Path("runs")


class CliError(RuntimeError):
    """A concise, safe-to-display CLI error."""


def _add_model_options(
    parser: argparse.ArgumentParser,
    *,
    repeatable: bool = False,
    default_policy: str = "production",
) -> None:
    parser.add_argument(
        "--model",
        action="append" if repeatable else "store",
        default=None,
        metavar="MODEL",
        help=(
            "review model (repeat for a model sweep)"
            if repeatable
            else f"review model (default: {DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("fast", "balanced"),
        default="balanced",
        help="fast uses generation plus deterministic gates; balanced adds a verifier",
    )
    parser.add_argument(
        "--review-policy",
        choices=("production", "codereviewbench"),
        default=default_policy,
        help=f"versioned finding-scope policy (default: {default_policy})",
    )
    parser.add_argument(
        "--verifier-model",
        default=None,
        metavar="MODEL|same|none",
        help="verifier model; default is same for balanced and none for fast",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="low",
        choices=("minimal", "low", "medium", "high", "xhigh"),
    )
    parser.add_argument(
        "--verifier-reasoning-effort",
        default="low",
        choices=("minimal", "low", "medium", "high", "xhigh"),
    )
    parser.add_argument(
        "--llm-concurrency",
        type=_positive_int,
        default=4,
        metavar="N",
        help="maximum concurrent model calls inside one review",
    )
    parser.add_argument(
        "--context-mode",
        choices=("curated", "agentic"),
        default="curated",
        help=(
            "curated uses deterministic repository evidence; agentic lets the model "
            "request bounded immutable reads and searches"
        ),
    )
    parser.add_argument(
        "--context-window-tokens",
        type=_positive_int,
        default=None,
        metavar="N",
        help="declared model context window used to derive reproducible per-model input bounds",
    )
    parser.add_argument(
        "--model-context-window",
        action="append",
        default=None,
        metavar="MODEL=N",
        help="override the declared context window for MODEL (repeat for model sweeps)",
    )
    parser.add_argument(
        "--verifier-context-window-tokens",
        type=_positive_int,
        default=None,
        metavar="N",
        help="declared context window for a pinned verifier (defaults to the review window for same)",
    )
    parser.add_argument("--diff-context-lines", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--max-chunk-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--max-context-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--initial-context-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--source-context-lines", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--max-symbols-per-chunk", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--max-hits-per-symbol", type=_positive_int, default=None, metavar="N")
    parser.add_argument(
        "--context-selection-rounds",
        type=_positive_int,
        default=None,
        metavar="N",
        help="model-directed selection rounds (default: 2; maximum: 8)",
    )
    parser.add_argument(
        "--context-requests-per-round", type=_positive_int, default=None, metavar="N"
    )
    parser.add_argument(
        "--max-context-files",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum distinct files added by agentic actions; curated seed files are separate",
    )
    parser.add_argument("--context-read-lines", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--context-read-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument(
        "--context-blob-read-bytes",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum immutable blob bytes fetched to satisfy one bounded line read",
    )
    parser.add_argument("--context-search-hits", type=_positive_int, default=None, metavar="N")
    parser.add_argument(
        "--context-search-max-offset",
        type=_positive_int,
        default=None,
        metavar="N",
        help="largest one-based result offset the model may request from a literal search",
    )
    parser.add_argument("--repository-index-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--verification-batch-size", type=_positive_int, default=None, metavar="N")
    parser.add_argument("--verification-batch-chars", type=_positive_int, default=None, metavar="N")
    parser.add_argument(
        "--verification-semantic-retries",
        type=_nonnegative_int,
        default=None,
        metavar="N",
        help="retry a structurally valid but semantically invalid verifier response (default: 2)",
    )
    operating_point = parser.add_mutually_exclusive_group()
    operating_point.add_argument(
        "--operating-point",
        type=Path,
        default=None,
        metavar="JSON",
        help="frozen verifier operating point produced by 'bugbunny calibrate'",
    )
    operating_point.add_argument(
        "--min-verifier-confidence",
        type=float,
        default=None,
        metavar="P",
        help="manual verifier threshold in [0,1] (not recommended for benchmark runs)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=32_768,
        metavar="N",
        help=("completion-token cap for Martian and planning reserve for Codex (default: 32768)"),
    )
    parser.add_argument(
        "--verifier-max-output-tokens",
        type=_positive_int,
        default=None,
        metavar="N",
        help="separate verifier completion cap (default: --max-output-tokens)",
    )


def _add_auth_options(parser: argparse.ArgumentParser) -> None:
    auth = parser.add_argument_group("Martian model authentication")
    credential = auth.add_mutually_exclusive_group()
    credential.add_argument(
        "--api-key",
        default=None,
        help="Martian API key (prefer .env or --api-key-env to keep it out of shell history)",
    )
    credential.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help=f"read the Martian API key from NAME (default: {MARTIAN_API_KEY_ENV})",
    )
    auth.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        metavar="PATH",
        help="fallback dotenv file containing MARTIAN_API_KEY (default: .env)",
    )
    auth.add_argument(
        "--api-base",
        default=None,
        metavar="URL",
        help=f"Martian-compatible base URL (default: {MARTIAN_API_BASE})",
    )
    auth.add_argument(
        "--codex-executable",
        default="codex",
        metavar="PATH",
        help="Codex CLI used by codex/* models and the logged-in ChatGPT session",
    )
    auth.add_argument("--timeout", type=_positive_int, default=300, metavar="SECONDS")


class _RedactingArgumentParser(argparse.ArgumentParser):
    """Redact credentials from argparse's own error channel.

    argparse prints usage errors and exits with SystemExit before main()'s
    redaction boundary can see them, so a credential mistyped into the wrong
    flag (for example an API key handed to --profile) would print verbatim
    into captured CI logs. Only environment- and argv-derived secrets are
    knowable this early, so this redaction is best-effort by construction.
    Subparsers inherit this class via argparse's parser_class default.
    """

    def error(self, message: str) -> Any:
        secrets = _argument_secrets(sys.argv[1:])
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_redact_text(message, secrets)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(
        prog="bugbunny",
        description="Fast, reproducible LLM code review and CodeReviewBench evaluation",
    )
    parser.add_argument("--version", action="version", version=_version_string())
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check local tools and auth availability")
    doctor.add_argument("--codex-executable", default="codex", metavar="PATH")
    doctor.add_argument("--env-file", type=Path, default=Path(".env"), metavar="PATH")
    doctor.add_argument(
        "--check-env",
        action="append",
        default=None,
        metavar="NAME",
        help="also report whether NAME is set (values are never printed)",
    )

    calibrate = subparsers.add_parser(
        "calibrate", help="freeze a verifier threshold on an external labeled corpus"
    )
    calibrate.add_argument("--corpus", type=Path, required=True)
    calibrate.add_argument("--output", type=Path, required=True)
    calibrate.add_argument(
        "--verifier-model", default="anthropic/claude-opus-4-5", metavar="PROVIDER/MODEL"
    )
    calibrate.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    calibrate.add_argument("--concurrency", type=_positive_int, default=8)
    calibrate.add_argument("--minimum-precision", type=float, default=0.80)
    calibrate.add_argument("--max-output-tokens", type=_positive_int, default=4_096)
    calibrate.add_argument(
        "--benchmark-data",
        type=Path,
        default=None,
        help="cross-check the corpus against this benchmark_data.json instead of "
        "trusting the corpus's self-declared exclusion attestation",
    )
    _add_auth_options(calibrate)

    review = subparsers.add_parser("review-pr", help="review one GitHub pull request locally")
    review.add_argument("url", help="https://github.com/OWNER/REPO/pull/NUMBER")
    _add_model_options(review)
    _add_auth_options(review)
    review.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    review.add_argument("--output", type=Path, default=Path("bugbunny-review.json"))
    review.add_argument(
        "--markdown",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write a human-readable Markdown report",
    )

    benchmark = subparsers.add_parser("benchmark", help="CodeReviewBench integration")
    benchmark_commands = benchmark.add_subparsers(dest="benchmark_command", required=True)

    run = benchmark_commands.add_parser(
        "run", help="run one or more models on cloned CodeReviewBench fixtures"
    )
    run.add_argument("--benchmark-data", type=Path, required=True)
    run.add_argument(
        "--expect-benchmark-sha256",
        default=None,
        metavar="HEX64",
        help="require benchmark_data.json to match this exact SHA-256 (the documented upstream pin)",
    )
    run.add_argument(
        "--fixture-tool",
        default="auto",
        metavar="TOOL|auto",
        help="fixture tool slug to require, or auto for deterministic selection",
    )
    _add_model_options(run, repeatable=True, default_policy="codereviewbench")
    _add_auth_options(run)
    run.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    run.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="run destination (default: a new timestamped directory under runs/)",
    )
    run.add_argument(
        "--filter",
        default=None,
        metavar="TEXT",
        help="select cases whose ID, repository, golden URL, or fixture URL contains TEXT",
    )
    run.add_argument("--limit", type=_positive_int, default=None)
    run.add_argument(
        "--concurrency",
        type=_positive_int,
        default=None,
        metavar="N",
        help="deprecated alias for --active-reviews",
    )
    run.add_argument(
        "--active-reviews",
        type=_positive_int,
        default=10,
        metavar="N",
        help="maximum prepared pull requests reviewed concurrently (default: 10)",
    )
    run.add_argument(
        "--global-llm-concurrency",
        type=_positive_int,
        default=16,
        metavar="N",
        help="maximum model calls across all models and reviews (default: 16)",
    )
    run.add_argument(
        "--github-concurrency",
        type=_positive_int,
        default=16,
        metavar="N",
        help="maximum concurrent GitHub PR resolutions (default: 16)",
    )
    run.add_argument(
        "--git-concurrency",
        type=_positive_int,
        default=4,
        metavar="N",
        help="maximum concurrent repository preparations (default: 4)",
    )
    run.add_argument(
        "--no-resume",
        action="store_true",
        help="rerun completed artifacts instead of reusing them",
    )

    export = benchmark_commands.add_parser(
        "export", help="export final findings into CodeReviewBench judge inputs"
    )
    export.add_argument("--benchmark-data", type=Path, required=True)
    artifact_source = export.add_mutually_exclusive_group(required=True)
    artifact_source.add_argument("--artifacts", type=Path, nargs="+", metavar="JSON")
    artifact_source.add_argument("--run-dir", type=Path)
    export.add_argument("--judge-model", required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    export.add_argument(
        "--finding-stage",
        action="append",
        choices=("generator", "balanced", "family"),
        default=None,
        help=("candidate track to export; repeat for multiple tracks (default: balanced)"),
    )

    verify_export = benchmark_commands.add_parser(
        "verify-export", help="verify a committed CodeReviewBench export bundle"
    )
    verify_export.add_argument("--manifest", type=Path, required=True)

    judge = benchmark_commands.add_parser(
        "judge", help="run CodeReviewBench Step 3 with bounded global concurrency"
    )
    judge.add_argument("--results-dir", type=Path, required=True)
    judge.add_argument("--judge-model", required=True)
    judge.add_argument(
        "--tool",
        action="append",
        default=None,
        help="tool ID to judge (repeat for multiple exported models)",
    )
    judge.add_argument("--judge-concurrency", type=_positive_int, default=20, metavar="N")
    judge.add_argument("--review-concurrency", type=_positive_int, default=10, metavar="N")
    judge.add_argument("--call-timeout", type=_positive_int, default=30, metavar="SECONDS")
    judge.add_argument("--review-timeout", type=_positive_int, default=1800, metavar="SECONDS")
    judge.add_argument(
        "--max-retries",
        type=_positive_int,
        default=5,
        metavar="N",
        help="total attempts for each judge comparison (default: 5)",
    )
    judge.add_argument("--force", action="store_true")
    judge.add_argument("--evaluations-file", type=Path, default=None)
    judge_auth = judge.add_argument_group("Martian judge authentication")
    judge_credential = judge_auth.add_mutually_exclusive_group()
    judge_credential.add_argument("--api-key", default=None)
    judge_credential.add_argument("--api-key-env", default=None, metavar="NAME")
    judge_auth.add_argument("--env-file", type=Path, default=Path(".env"), metavar="PATH")
    judge_auth.add_argument("--api-base", default=None, metavar="URL")

    analyze = benchmark_commands.add_parser(
        "analyze", help="audit completed runs, tracks, thresholds, and uncertainty"
    )
    analyze.add_argument("--run-dir", type=Path, required=True)
    analyze.add_argument("--results-dir", type=Path, required=True)
    analyze.add_argument("--judge-model", required=True)
    analyze.add_argument("--output-json", type=Path, default=None)
    analyze.add_argument("--bootstrap-samples", type=_positive_int, default=2_000)
    analyze.add_argument("--bootstrap-seed", type=int, default=17_042)
    analyze.add_argument(
        "--allow-judge-errors",
        action="store_true",
        help="exclude (rather than fail on) judge-error-degraded evaluations",
    )

    publish = subparsers.add_parser(
        "publish", help="explicitly publish a completed artifact as one GitHub review"
    )
    publish.add_argument("artifact", type=Path)
    confirmation = publish.add_mutually_exclusive_group()
    confirmation.add_argument(
        "--confirm-publish",
        action="store_true",
        help="confirm the externally visible GitHub write",
    )
    confirmation.add_argument("--yes", action="store_true", help="alias for --confirm-publish")
    publish.add_argument(
        "--publish-clean",
        action="store_true",
        help="publish a clean review even when there are no findings",
    )
    publish.add_argument(
        "--github-token-env",
        default=None,
        metavar="NAME",
        help="read a GitHub token from NAME instead of GITHUB_TOKEN/GH_TOKEN",
    )
    return parser


def _version_string() -> str:
    try:
        version = importlib.metadata.version("bugbunny")
    except importlib.metadata.PackageNotFoundError:
        from bugbunny import __version__

        version = __version__
    return f"BugBunny {version}"


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def _model_context_windows(args: argparse.Namespace) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in getattr(args, "model_context_window", None) or []:
        model, separator, token_text = raw.rpartition("=")
        if not separator or not model.strip():
            raise CliError("--model-context-window must use MODEL=N")
        try:
            tokens = int(token_text)
        except ValueError as exc:
            raise CliError("--model-context-window must use a positive integer") from exc
        model = model.strip()
        if tokens <= 0:
            raise CliError("--model-context-window must use a positive integer")
        if model in result:
            raise CliError(f"duplicate --model-context-window for {model}")
        result[model] = tokens
    return result


def _validate_context_window_models(args: argparse.Namespace, models: Sequence[str]) -> None:
    unknown = sorted(set(_model_context_windows(args)) - set(models))
    if unknown:
        raise CliError("--model-context-window names an unselected model: " + ", ".join(unknown))


def _resolved_context_config(
    args: argparse.Namespace,
    *,
    model: str,
) -> dict[str, Any]:
    defaults = ReviewConfig()
    declared_window = _model_context_windows(args).get(
        model, getattr(args, "context_window_tokens", None)
    )
    requested_output = int(args.max_output_tokens)
    if declared_window is None:
        max_output_tokens = requested_output
        generation_input_char_budget = None
        max_chunk_chars = args.max_chunk_chars or defaults.max_chunk_chars
        max_context_chars = args.max_context_chars or defaults.max_context_chars
        budget_source = "fixed"
    else:
        if declared_window < 16_384:
            raise CliError("declared context windows must be at least 16384 tokens")
        max_output_tokens = min(requested_output, max(2_048, declared_window // 4))
        input_tokens = declared_window - max_output_tokens - DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
        if input_tokens <= 0:
            raise CliError("declared context window leaves no room for model input")
        # This is a deterministic planning estimate, not provider tokenization.
        # Exact rendered prompt characters and provider-reported tokens are
        # recorded so experiments can detect a poor estimate post-hoc.
        generation_input_char_budget = input_tokens * DECLARED_WINDOW_CHARS_PER_TOKEN
        evidence_chars = generation_input_char_budget - DECLARED_GENERATION_FRAMING_CHARS
        if evidence_chars < 16_384:
            raise CliError(
                "declared context window is too small for the minimum patch/context envelope"
            )
        max_chunk_chars = args.max_chunk_chars or max(8_192, evidence_chars * 2 // 5)
        max_context_chars = args.max_context_chars or max(8_192, evidence_chars - max_chunk_chars)
        budget_source = "declared_window"
    initial_context_chars = args.initial_context_chars
    if initial_context_chars is None:
        agentic_seed_target = max(4_000, max_context_chars // 3)
        initial_context_chars = min(
            defaults.initial_context_chars,
            max_context_chars,
            agentic_seed_target if args.context_mode == "agentic" else max_context_chars,
        )
    if initial_context_chars > max_context_chars:
        raise CliError("--initial-context-chars cannot exceed --max-context-chars")
    if declared_window is None:
        context_requests_per_round = (
            args.context_requests_per_round or defaults.context_requests_per_round
        )
        max_context_files = args.max_context_files or defaults.max_context_files
        context_read_chars = args.context_read_chars or defaults.context_read_chars
        context_search_hits = args.context_search_hits or defaults.context_search_hits
        repository_index_chars = args.repository_index_chars or defaults.repository_index_chars
    else:
        # These are deterministic functions of the user-declared window. They
        # are intentionally frozen into ReviewConfig instead of inferred from
        # a mutable provider catalog or a model-name heuristic.
        max_context_files = args.max_context_files or max(4, min(64, max_context_chars // 8_000))
        context_requests_per_round = args.context_requests_per_round or max(
            4, min(16, (max_context_files + 1) // 2)
        )
        context_read_chars = args.context_read_chars or max(
            4_000, min(48_000, max_context_chars // 4)
        )
        context_search_hits = args.context_search_hits or max(6, min(24, max_context_files))
        repository_index_chars = args.repository_index_chars or max(
            8_000, min(120_000, max_context_chars // 2)
        )
    return {
        "context_mode": args.context_mode,
        "context_budget_source": budget_source,
        "context_window_tokens": declared_window,
        "generation_input_char_budget": generation_input_char_budget,
        "diff_context_lines": args.diff_context_lines or defaults.diff_context_lines,
        "max_chunk_chars": max_chunk_chars,
        "max_context_chars": max_context_chars,
        "initial_context_chars": initial_context_chars,
        "source_context_lines": args.source_context_lines or defaults.source_context_lines,
        "max_symbols_per_chunk": (args.max_symbols_per_chunk or defaults.max_symbols_per_chunk),
        "max_hits_per_symbol": args.max_hits_per_symbol or defaults.max_hits_per_symbol,
        "context_selection_rounds": (
            args.context_selection_rounds or defaults.context_selection_rounds
        ),
        "context_requests_per_round": context_requests_per_round,
        "max_context_files": max_context_files,
        "context_read_lines": args.context_read_lines or defaults.context_read_lines,
        "context_read_chars": context_read_chars,
        "context_blob_read_bytes": (
            args.context_blob_read_bytes or defaults.context_blob_read_bytes
        ),
        "context_search_hits": context_search_hits,
        "context_search_max_offset": (
            args.context_search_max_offset or defaults.context_search_max_offset
        ),
        "repository_index_chars": repository_index_chars,
        "max_output_tokens": max_output_tokens,
    }


def _review_config(args: argparse.Namespace, *, model: str | None = None) -> ReviewConfig:
    from bugbunny.policy import get_review_policy

    selected_model = model or getattr(args, "model", None) or DEFAULT_MODEL
    verifier = getattr(args, "verifier_model", None)
    profile = str(args.profile)
    if verifier is None:
        verifier = None if profile == "fast" else "same"
    elif verifier.lower() == "none":
        verifier = None
    if profile == "fast" and verifier is not None:
        raise CliError("--profile fast cannot be combined with a verifier model")
    policy = get_review_policy(str(args.review_policy))
    threshold = ReviewConfig().min_verifier_confidence
    operating_point_id = None
    operating_point_sha256 = None
    operating_point_path = getattr(args, "operating_point", None)
    if operating_point_path is not None:
        from bugbunny.calibration import load_operating_point

        operating_point, operating_point_sha256 = load_operating_point(operating_point_path)
        if verifier in {None, "none"}:
            raise CliError("--operating-point requires an enabled verifier")
        expected_verifier = selected_model if verifier == "same" else verifier
        if operating_point["verifier_model"] != expected_verifier:
            raise CliError("operating point verifier_model does not match --verifier-model")
        if operating_point["reasoning_effort"] != args.verifier_reasoning_effort:
            raise CliError(
                "operating point reasoning_effort does not match --verifier-reasoning-effort"
            )
        threshold = float(operating_point["threshold"])
        operating_point_id = str(operating_point["operating_point_id"])
    elif getattr(args, "min_verifier_confidence", None) is not None:
        threshold = float(args.min_verifier_confidence)
        if not 0 <= threshold <= 1:
            raise CliError("--min-verifier-confidence must be between 0 and 1")
    context_config = _resolved_context_config(args, model=selected_model)
    review_window = context_config["context_window_tokens"]
    verifier_window = getattr(args, "verifier_context_window_tokens", None)
    verifier_output_override = getattr(args, "verifier_max_output_tokens", None)
    if verifier is None:
        if verifier_window is not None:
            raise CliError("--verifier-context-window-tokens requires an enabled verifier")
        if verifier_output_override is not None:
            raise CliError("--verifier-max-output-tokens requires an enabled verifier")
    elif verifier == "same":
        verifier_window = verifier_window or review_window
    elif review_window is not None and verifier_window is None:
        raise CliError(
            "a pinned verifier needs --verifier-context-window-tokens when review windows are declared"
        )

    requested_verifier_output = int(verifier_output_override or args.max_output_tokens)
    if verifier_window is None:
        verifier_max_output_tokens = (
            int(context_config["max_output_tokens"])
            if verifier is None
            else requested_verifier_output
        )
        verifier_input_char_budget = None
    else:
        if verifier_window < 16_384:
            raise CliError("declared verifier context windows must be at least 16384 tokens")
        verifier_max_output_tokens = min(
            requested_verifier_output, max(2_048, verifier_window // 4)
        )
        verifier_input_tokens = (
            verifier_window - verifier_max_output_tokens - DECLARED_WINDOW_PROTOCOL_RESERVE_TOKENS
        )
        if verifier_input_tokens <= 0:
            raise CliError("declared verifier context window leaves no room for model input")
        verifier_input_char_budget = verifier_input_tokens * DECLARED_WINDOW_CHARS_PER_TOKEN
    config = ReviewConfig(
        model=selected_model,
        verifier_model=verifier,
        profile=profile,  # type: ignore[arg-type]
        review_policy=policy.name,
        review_policy_version=policy.version,
        review_policy_sha256=policy.sha256,
        reasoning_effort=args.reasoning_effort,
        verifier_reasoning_effort=args.verifier_reasoning_effort,
        llm_concurrency=args.llm_concurrency,
        verification_batch_size=(
            args.verification_batch_size or ReviewConfig().verification_batch_size
        ),
        verification_batch_chars=(
            args.verification_batch_chars or ReviewConfig().verification_batch_chars
        ),
        verification_semantic_retries=(
            args.verification_semantic_retries
            if args.verification_semantic_retries is not None
            else ReviewConfig().verification_semantic_retries
        ),
        timeout_seconds=args.timeout,
        verifier_context_window_tokens=verifier_window,
        verifier_input_char_budget=verifier_input_char_budget,
        verifier_max_output_tokens=verifier_max_output_tokens,
        min_verifier_confidence=threshold,
        operating_point_id=operating_point_id,
        operating_point_sha256=operating_point_sha256,
        include_categories=policy.categories,  # type: ignore[arg-type]
        **context_config,
    )
    config.validate()
    return config


def _gateway_config(
    args: argparse.Namespace,
    *,
    max_output_tokens: int | None = None,
) -> GatewayConfig:
    api_key_env = getattr(args, "api_key_env", None)
    if api_key_env is not None and not api_key_env.strip():
        raise CliError("--api-key-env must not be empty")
    config = GatewayConfig(
        api_key=getattr(args, "api_key", None),
        api_key_env=api_key_env,
        api_base=getattr(args, "api_base", None),
        dotenv_path=getattr(args, "env_file", None),
        timeout_seconds=args.timeout,
        max_output_tokens=max_output_tokens or args.max_output_tokens,
        codex_executable=args.codex_executable,
    )
    _register_runtime_secret(config.resolved_api_key())
    _register_runtime_secret(config.api_base)
    return config


def _safe_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return dict(mapped)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"expected an artifact-like object, got {type(value).__name__}")


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _redact_text(value: str, secrets: Iterable[str | None]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


# Secrets resolved at runtime (for example from an ignored .env file) never
# appear on the command line, so the argv scan below cannot recover them.
# Credential-resolving call sites register them here for the final redaction
# boundary in main().
_RUNTIME_SECRETS: set[str] = set()


def _register_runtime_secret(value: str | None) -> None:
    if value:
        _RUNTIME_SECRETS.add(value)


def _argument_secrets(argv: Sequence[str]) -> list[str]:
    secrets = [
        os.environ.get("MARTIAN_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("ANTHROPIC_API_KEY"),
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GITHUB_TOKEN"),
        os.environ.get("GH_TOKEN"),
        *_RUNTIME_SECRETS,
    ]

    sensitive_flags = ("--api-key", "--api-base", "--api-key-env", "--github-token-env")

    for index, item in enumerate(argv):
        flag, separator, inline_value = item.partition("=")
        # argparse accepts unambiguous prefix abbreviations ("--api-k"), so an
        # exact-string scan would leave abbreviated secret flags unredacted.
        # Redacting both the literal value and its environment lookup for any
        # prefix match over-approximates harmlessly.
        if len(flag) < 6 or not any(name.startswith(flag) for name in sensitive_flags):
            continue
        value = inline_value if separator else (argv[index + 1] if index + 1 < len(argv) else None)
        if value is None:
            continue
        secrets.append(value)
        secrets.append(os.environ.get(value))
    return [secret for secret in secrets if secret]


def _engine_types() -> tuple[Any, Any]:
    from bugbunny.engine import ReviewEngine, write_review_artifact

    return ReviewEngine, write_review_artifact


def _review_runtime(gateway: ModelGateway, config: ReviewConfig) -> dict[str, Any]:
    from bugbunny.engine import review_runtime_provenance

    return review_runtime_provenance(
        gateway,
        model=config.model,
        verifier_model=config.verifier_model,
        generation_reasoning_effort=config.reasoning_effort,
        verifier_reasoning_effort=config.verifier_reasoning_effort,
        generation_max_output_tokens=config.max_output_tokens,
        verifier_max_output_tokens=config.verifier_max_output_tokens,
    )


def _repository_type() -> Any:
    from bugbunny.repository import GitRepositoryCache

    return GitRepositoryCache


def _github_types() -> tuple[Any, Any]:
    from bugbunny.github import GitHubClient, GitHubReviewPublisher

    return GitHubClient, GitHubReviewPublisher


def _benchmark_api() -> tuple[Any, Any, Any, Any]:
    from bugbunny.benchmark import (
        artifact_model_directory,
        export_codereviewbench_results,
        load_codereviewbench_dataset,
        sanitize_model_name,
    )

    return (
        load_codereviewbench_dataset,
        export_codereviewbench_results,
        sanitize_model_name,
        artifact_model_directory,
    )


def _doctor(args: argparse.Namespace) -> int:
    git_path = shutil.which("git")
    git_ok = bool(git_path)
    git_version: str | None = None
    if git_path:
        try:
            process = subprocess.run(
                [git_path, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            git_ok = process.returncode == 0
            if git_ok:
                git_version = process.stdout.strip().splitlines()[0][:120]
        except (OSError, subprocess.TimeoutExpired):
            git_ok = False

    httpx_ok = False
    httpx_version: str | None = None
    try:
        httpx_version = importlib.metadata.version("httpx")
        httpx_ok = True
    except importlib.metadata.PackageNotFoundError:
        pass

    codex_path = shutil.which(args.codex_executable)
    codex_logged_in = False
    if codex_path:
        try:
            process = subprocess.run(
                [codex_path, "login", "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            codex_logged_in = process.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass

    env_names = {MARTIAN_API_KEY_ENV, "GITHUB_TOKEN", "GH_TOKEN"} | set(args.check_env or [])
    env_present = {name: bool(os.environ.get(name)) for name in sorted(env_names)}
    martian_key_configured = GatewayConfig(dotenv_path=args.env_file).resolved_api_key() is not None
    model_auth_available = codex_logged_in or martian_key_configured
    report = {
        "ok": git_ok and httpx_ok and model_auth_available,
        "git": {"available": git_ok, "path": git_path, "version": git_version},
        "martian": {
            "available": httpx_ok,
            "api_host": "api.withmartian.com",
            "credential_configured": martian_key_configured,
            "httpx_version": httpx_version,
        },
        "codex": {"available": bool(codex_path), "path": codex_path, "logged_in": codex_logged_in},
        "environment": {name: {"present": present} for name, present in env_present.items()},
    }
    _print_json(report)
    return 0 if report["ok"] else 1


async def _review_pr(args: argparse.Namespace) -> int:
    ReviewEngine, write_review_artifact = _engine_types()
    GitRepositoryCache = _repository_type()
    GitHubClient, _publisher = _github_types()
    selected_model = getattr(args, "model", None) or DEFAULT_MODEL
    _validate_context_window_models(args, [selected_model])
    config = _review_config(args)
    cache = GitRepositoryCache(args.cache_dir)
    with GitHubClient() as github:
        pr = await asyncio.to_thread(github.resolve_pr, args.url)
    async with ModelGateway(
        _gateway_config(args, max_output_tokens=config.max_output_tokens)
    ) as gateway:
        artifact = await ReviewEngine(config, gateway, cache).review(pr)
    written = write_review_artifact(artifact, args.output, markdown_path=args.markdown)
    result = {
        "status": artifact.status,
        "artifact": str(args.output.expanduser().resolve()),
        "markdown": str(args.markdown.expanduser().resolve()) if args.markdown else None,
        "findings": len(artifact.findings),
        "run_id": artifact.run_id,
    }
    if written is not None and isinstance(written, (str, Path)):
        result["artifact"] = str(Path(written).expanduser().resolve())
    _print_json(result)
    return 0 if artifact.status == "completed" else 1


def _matches_case(case: Any, needle: str | None) -> bool:
    if not needle:
        return True
    lowered = needle.casefold()
    values = (
        case.case_id,
        case.repository,
        case.golden_url,
        case.review_url,
        case.fixture_repo_name,
    )
    return any(lowered in str(value).casefold() for value in values)


def _github_auth_token() -> str | None:
    """Use explicit environment auth, then the current gh login, without persistence."""

    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return token
    executable = shutil.which("gh")
    if executable is None and Path("/opt/homebrew/bin/gh").is_file():
        executable = "/opt/homebrew/bin/gh"
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    if result.returncode == 0 and token:
        # The gh-login credential exists nowhere in argv or the environment;
        # without registration the main() redaction boundary cannot cover an
        # exception that happens to embed it.
        _register_runtime_secret(token)
        return token
    return None


def _pr_plan_value(pr: Any) -> dict[str, Any]:
    if isinstance(pr, PRInfo):
        return pr.to_dict()
    value = _safe_mapping(pr)
    missing = [name for name in PRInfo.__dataclass_fields__ if name not in value]
    if missing:
        raise CliError(f"resolved pull request is missing immutable fields: {', '.join(missing)}")
    return {name: value[name] for name in PRInfo.__dataclass_fields__}


def _completed_artifact(
    path: Path,
    *,
    config: ReviewConfig,
    case_id: str,
    review_url: str,
    golden_sha256: str,
    benchmark_sha256: str,
    dataset_golden_sha256: str,
    base_sha: str,
    head_sha: str,
    runtime: Mapping[str, Any],
    expected_sha256: str | None,
) -> bool:
    if (
        not path.is_file()
        or not expected_sha256
        or sha256_bytes(path.read_bytes()) != expected_sha256
    ):
        return False
    try:
        from bugbunny import __version__
        from bugbunny.exploration import exploration_prompt_sha256, exploration_schema_sha256
        from bugbunny.prompts import generation_prompt_sha256, verifier_prompt_sha256

        value = load_json(path)
        benchmark = value.get("benchmark") if isinstance(value, Mapping) else None
        pr = value.get("pr") if isinstance(value, Mapping) else None
        context = value.get("context") if isinstance(value, Mapping) else None
        coverage = value.get("coverage") if isinstance(value, Mapping) else None
        diff = value.get("diff") if isinstance(value, Mapping) else None
        return bool(
            isinstance(value, Mapping)
            and value.get("status") == "completed"
            and value.get("schema_version") == REVIEW_SCHEMA_VERSION
            and value.get("tool_version") == __version__
            and value.get("implementation") == implementation_identity()
            and isinstance(value.get("config"), Mapping)
            and dict(value["config"]) == config.to_dict()
            and isinstance(value.get("runtime"), Mapping)
            and dict(value["runtime"]) == dict(runtime)
            and isinstance(benchmark, Mapping)
            and benchmark.get("case_id") == case_id
            and benchmark.get("review_url") == review_url
            and benchmark.get("golden_sha256") == golden_sha256
            and benchmark.get("benchmark_sha256") == benchmark_sha256
            and benchmark.get("dataset_golden_sha256") == dataset_golden_sha256
            and isinstance(pr, Mapping)
            and pr.get("url") == review_url
            and pr.get("base_sha") == base_sha
            and pr.get("head_sha") == head_sha
            and isinstance(context, Mapping)
            and context.get("generation_prompt_sha256")
            == generation_prompt_sha256(config.review_policy, config.include_categories)
            and context.get("verifier_prompt_sha256") == verifier_prompt_sha256()
            and context.get("context_selection_prompt_sha256") == exploration_prompt_sha256()
            and context.get("context_selection_schema_sha256")
            == exploration_schema_sha256(
                config.context_requests_per_round,
                config.context_search_max_offset,
            )
            and isinstance(coverage, Mapping)
            and coverage.get("complete") is True
            and isinstance(diff, Mapping)
            and diff.get("chunk_plan_complete") is True
            and isinstance(diff.get("commentable_ranges"), Mapping)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


class _BoundedAcquireCache:
    """Apply --git-concurrency to review-phase acquisitions as well.

    Prewarming honors the git semaphore, but a case whose preparation failed
    (or was skipped) re-fetches inside the review job through the engine's
    ``asyncio.to_thread`` boundary; a threading semaphore bounds those too.
    """

    def __init__(self, cache: Any, limit: int) -> None:
        self._cache = cache
        self._semaphore = threading.Semaphore(limit)

    def acquire(self, pr: Any) -> Any:
        with self._semaphore:
            return self._cache.acquire(pr)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cache, name)


@contextmanager
def _run_dir_lock(run_root: Path) -> Iterator[None]:
    """Hold an exclusive run-directory lock for the whole benchmark run.

    Two concurrent runs (for example an accidental double resume) would
    interleave checkpoint rewrites and silently lose completed records.
    """

    import fcntl

    run_root.mkdir(parents=True, exist_ok=True)
    with (run_root / ".bugbunny-run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CliError(
                f"run directory is already in use by another BugBunny process: {run_root}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def _benchmark_run(args: argparse.Namespace) -> int:
    default_run_name = utc_now().replace("-", "").replace(":", "").replace(".", "-")
    run_dir = args.run_dir or (DEFAULT_RUNS_DIR / default_run_name)
    run_root = run_dir.expanduser().resolve()
    with _run_dir_lock(run_root):
        return await _benchmark_run_locked(args, run_root)


async def _benchmark_run_locked(args: argparse.Namespace, run_root: Path) -> int:
    load_dataset, _export, _sanitize_model_name, artifact_model_directory = _benchmark_api()
    ReviewEngine, write_review_artifact = _engine_types()
    GitRepositoryCache = _repository_type()
    GitHubClient, _publisher = _github_types()

    dataset = load_dataset(
        args.benchmark_data,
        preferred_fixture_tool=args.fixture_tool,
        require_preferred_tool=True,
        expected_case_count=50,
        expected_benchmark_sha256=getattr(args, "expect_benchmark_sha256", None),
    )
    cases = [case for case in dataset.cases if _matches_case(case, args.filter)]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise CliError("the benchmark selection contains no cases")
    models = args.model or [DEFAULT_MODEL]
    if len(set(models)) != len(models):
        raise CliError("--model values must be unique")
    _validate_context_window_models(args, models)

    active_reviews = args.concurrency or args.active_reviews
    cache = GitRepositoryCache(args.cache_dir, shard_by_remote=True)
    selected_configs = {model: _review_config(args, model=model) for model in models}
    review_configs = {model: config.to_dict() for model, config in selected_configs.items()}
    gateway_config = _gateway_config(
        args,
        max_output_tokens=max(
            max(config.max_output_tokens, config.verifier_max_output_tokens)
            for config in selected_configs.values()
        ),
    )
    gateway = ModelGateway(gateway_config, max_concurrency=args.global_llm_concurrency)
    gateways = dict.fromkeys(models, gateway)
    runtime_provenance = {
        model: _review_runtime(gateways[model], selected_configs[model]) for model in models
    }
    selection = {"filter": args.filter, "limit": args.limit, "case_count": len(cases)}
    scheduler = {
        "active_reviews": active_reviews,
        "global_llm_concurrency": args.global_llm_concurrency,
        "per_review_llm_concurrency": args.llm_concurrency,
        "github_concurrency": args.github_concurrency,
        "git_concurrency": args.git_concurrency,
        "models_concurrent": True,
        "ordering": "largest-prepared-diff-first",
        "repository_cache": "per-remote",
        "git_http_version": "HTTP/1.1",
    }

    plan_path = run_root / "job_plan.json"
    plan_identity = {
        "implementation": implementation_identity(),
        "benchmark": dataset.manifest.to_dict(),
        "models": models,
        "review_configs": review_configs,
        "runtime_provenance": runtime_provenance,
        "fixture_tool": args.fixture_tool,
        "selection": selection,
    }
    if plan_path.is_file():
        try:
            plan = load_json(plan_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CliError(f"cannot read frozen job plan: {exc}") from exc
        if not (
            isinstance(plan, Mapping)
            and plan.get("schema_version") == BENCHMARK_PLAN_SCHEMA_VERSION
            and all(plan.get(key) == value for key, value in plan_identity.items())
        ):
            raise CliError("run directory belongs to a different frozen benchmark job plan")
        raw_resolved = plan.get("resolved_prs")
        if not isinstance(raw_resolved, Mapping) or set(raw_resolved) != {
            case.case_id for case in cases
        }:
            raise CliError("frozen job plan does not contain every selected case")
        try:
            resolved_prs = {
                case_id: PRInfo.from_dict(dict(value))
                for case_id, value in raw_resolved.items()
                if isinstance(case_id, str) and isinstance(value, Mapping)
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise CliError(f"frozen job plan has invalid pull-request metadata: {exc}") from exc
        if set(resolved_prs) != {case.case_id for case in cases}:
            raise CliError("frozen job plan has malformed resolved pull requests")
    else:
        resolved_prs: dict[str, PRInfo] = {}
        github_semaphore = asyncio.Semaphore(args.github_concurrency)
        github_token = _github_auth_token()
        with GitHubClient(token=github_token) as github:

            async def resolve_case(case: Any) -> None:
                try:
                    async with github_semaphore:
                        value = await asyncio.to_thread(github.resolve_pr, case.review_url)
                    resolved_prs[case.case_id] = PRInfo.from_dict(_pr_plan_value(value))
                except Exception as exc:
                    message = _redact_text(
                        f"{type(exc).__name__}: {exc}",
                        [
                            github_token,
                            os.environ.get("GITHUB_TOKEN"),
                            os.environ.get("GH_TOKEN"),
                        ],
                    )
                    raise CliError(
                        f"cannot resolve benchmark case {case.case_id}: {message}"
                    ) from exc

            # Let every resolution settle before the client closes; a fail-fast
            # gather would abandon in-flight tasks against a closing client.
            outcomes = await asyncio.gather(
                *(resolve_case(case) for case in cases), return_exceptions=True
            )
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            if failures:
                raise failures[0]
        plan = {
            "schema_version": BENCHMARK_PLAN_SCHEMA_VERSION,
            "created_at": utc_now(),
            **plan_identity,
            "resolved_prs": {
                case_id: resolved_prs[case_id].to_dict() for case_id in sorted(resolved_prs)
            },
        }
        atomic_write_json(plan_path, plan)

    plan_sha256 = sha256_bytes(plan_path.read_bytes())
    resolved_inputs = {
        case.case_id: {
            "review_url": case.review_url,
            "base_sha": resolved_prs[case.case_id].base_sha,
            "head_sha": resolved_prs[case.case_id].head_sha,
        }
        for case in sorted(cases, key=lambda selected: selected.case_id)
    }
    expected_jobs = {(model, case.case_id) for case in cases for model in models}

    records_by_job: dict[tuple[str, str], dict[str, Any]] = {}
    existing_artifact_hashes: dict[tuple[str, str], str] = {}
    existing_manifest_path = run_root / "run_manifest.json"
    if existing_manifest_path.is_file():
        try:
            existing_manifest = load_json(existing_manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CliError(f"cannot safely reuse run directory: {exc}") from exc
        if not (
            isinstance(existing_manifest, Mapping)
            and existing_manifest.get("schema_version") == BENCHMARK_RUN_SCHEMA_VERSION
            and existing_manifest.get("job_plan_sha256") == plan_sha256
            and existing_manifest.get("models") == models
        ):
            raise CliError("run manifest does not match its frozen job plan")
        for record in existing_manifest.get("records", []):
            if (
                isinstance(record, Mapping)
                and isinstance(record.get("model"), str)
                and isinstance(record.get("case_id"), str)
            ):
                key = (str(record["model"]), str(record["case_id"]))
                if key not in expected_jobs:
                    raise CliError("run manifest contains a record outside its frozen job plan")
                records_by_job[key] = dict(record)
                if isinstance(record.get("artifact_sha256"), str):
                    existing_artifact_hashes[key] = str(record["artifact_sha256"])

    checkpoint_path = run_root / "run_checkpoint.json"
    if checkpoint_path.is_file():
        try:
            checkpoint = load_json(checkpoint_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CliError(f"cannot read run checkpoint: {exc}") from exc
        if not (
            isinstance(checkpoint, Mapping)
            and checkpoint.get("schema_version") == "bugbunny-benchmark-checkpoint-v1"
            and checkpoint.get("job_plan_sha256") == plan_sha256
            and isinstance(checkpoint.get("records"), list)
        ):
            raise CliError("run checkpoint does not match its frozen job plan")
        for record in checkpoint["records"]:
            if not (
                isinstance(record, Mapping)
                and isinstance(record.get("model"), str)
                and isinstance(record.get("case_id"), str)
            ):
                raise CliError("run checkpoint contains an invalid record")
            key = (str(record["model"]), str(record["case_id"]))
            if key not in expected_jobs:
                raise CliError("run checkpoint contains a record outside its frozen job plan")
            records_by_job[key] = dict(record)
            if isinstance(record.get("artifact_sha256"), str):
                existing_artifact_hashes[key] = str(record["artifact_sha256"])

    reusable_jobs: set[tuple[str, str]] = set()
    if not args.no_resume:
        for case in cases:
            pr = resolved_prs[case.case_id]
            for model in models:
                artifact_path = (
                    run_root
                    / "artifacts"
                    / artifact_model_directory(model)
                    / f"{case.case_id}.json"
                )
                if _completed_artifact(
                    artifact_path,
                    config=selected_configs[model],
                    case_id=case.case_id,
                    review_url=case.review_url,
                    golden_sha256=case.golden_sha256,
                    benchmark_sha256=dataset.manifest.benchmark_sha256,
                    dataset_golden_sha256=dataset.manifest.golden_sha256,
                    base_sha=pr.base_sha,
                    head_sha=pr.head_sha,
                    runtime=runtime_provenance[model],
                    expected_sha256=existing_artifact_hashes.get((model, case.case_id)),
                ):
                    reusable_jobs.add((model, case.case_id))

    cases_to_prepare = [
        case
        for case in cases
        if any((model, case.case_id) not in reusable_jobs for model in models)
    ]
    preparation_hints: dict[str, int] = {}
    preparation_failures: dict[str, str] = {}
    git_semaphore = asyncio.Semaphore(args.git_concurrency)

    async def prepare_case(case: Any) -> None:
        try:
            async with git_semaphore:
                prepared = await asyncio.to_thread(cache.prepare, resolved_prs[case.case_id])
            preparation_hints[case.case_id] = int(prepared.diff_bytes)
        except Exception as exc:
            preparation_hints[case.case_id] = 0
            preparation_failures[case.case_id] = _redact_text(
                f"{type(exc).__name__}: {exc}",
                [
                    os.environ.get("GITHUB_TOKEN"),
                    os.environ.get("GH_TOKEN"),
                    gateway_config.resolved_api_key(),
                ],
            )

    await asyncio.gather(*(prepare_case(case) for case in cases_to_prepare))

    review_cache = _BoundedAcquireCache(cache, args.git_concurrency)
    engines = {
        model: ReviewEngine(selected_configs[model], gateway, review_cache) for model in models
    }
    ordered_cases = sorted(
        cases,
        key=lambda case: (-preparation_hints.get(case.case_id, 0), case.case_id),
    )
    jobs = [
        (model, case)
        for case in ordered_cases
        for model in models
        if (model, case.case_id) not in reusable_jobs
    ] + [
        (model, case)
        for case in sorted(cases, key=lambda selected: selected.case_id)
        for model in models
        if (model, case.case_id) in reusable_jobs
    ]
    review_semaphore = asyncio.Semaphore(active_reviews)
    record_lock = asyncio.Lock()

    async def commit_record(record: dict[str, Any]) -> None:
        async with record_lock:
            key = (str(record["model"]), str(record["case_id"]))
            records_by_job[key] = record
            checkpoint = {
                "schema_version": "bugbunny-benchmark-checkpoint-v1",
                "updated_at": utc_now(),
                "job_plan_sha256": plan_sha256,
                "records": sorted(
                    records_by_job.values(),
                    key=lambda item: (str(item["model"]), str(item["case_id"])),
                ),
            }
            await asyncio.to_thread(atomic_write_json, checkpoint_path, checkpoint)

    async def review_job(model: str, case: Any) -> None:
        artifact_dir = run_root / "artifacts" / artifact_model_directory(model)
        artifact_path = artifact_dir / f"{case.case_id}.json"
        markdown_path = artifact_dir / f"{case.case_id}.md"
        try:
            async with review_semaphore:
                pr = resolved_prs[case.case_id]
                reusable = (model, case.case_id) in reusable_jobs
                artifact = None if reusable else await engines[model].review(pr)
            if artifact is None:
                record = {
                    "case_id": case.case_id,
                    "model": model,
                    "status": "resumed",
                    "artifact": str(artifact_path.relative_to(run_root)),
                    "artifact_sha256": await asyncio.to_thread(
                        lambda: sha256_bytes(artifact_path.read_bytes())
                    ),
                }
            else:
                artifact.benchmark = {
                    "suite": "CodeReviewBench",
                    "case_id": case.case_id,
                    "golden_url": case.golden_url,
                    "review_url": case.review_url,
                    "fixture_tool": case.fixture_tool,
                    "golden_sha256": case.golden_sha256,
                    "benchmark_sha256": dataset.manifest.benchmark_sha256,
                    "dataset_golden_sha256": dataset.manifest.golden_sha256,
                }
                await asyncio.to_thread(
                    write_review_artifact,
                    artifact,
                    artifact_path,
                    markdown_path=markdown_path,
                )
                record = {
                    "case_id": case.case_id,
                    "model": model,
                    "status": artifact.status,
                    "artifact": str(artifact_path.relative_to(run_root)),
                    "findings": len(artifact.findings),
                    "artifact_sha256": await asyncio.to_thread(
                        lambda: sha256_bytes(artifact_path.read_bytes())
                    ),
                }
        except Exception as exc:
            record = {
                "case_id": case.case_id,
                "model": model,
                "status": "failed",
                "error": _redact_text(
                    f"{type(exc).__name__}: {exc}",
                    [
                        getattr(args, "api_key", None),
                        os.environ.get(getattr(args, "api_key_env", "") or ""),
                        gateway_config.resolved_api_key(),
                        getattr(args, "api_base", None),
                        os.environ.get("GITHUB_TOKEN"),
                        os.environ.get("GH_TOKEN"),
                    ],
                ),
            }
        await commit_record(record)

    try:
        # Settle every job before the gateway closes and the run-dir lock
        # releases: a fail-fast gather (a commit_record OSError escapes
        # review_job's inner handler) would abandon in-flight jobs whose
        # executor-thread checkpoint and artifact writes could land after
        # another process re-acquires the run directory.
        outcomes = await asyncio.gather(
            *(review_job(model, case) for model, case in jobs), return_exceptions=True
        )
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        if failures:
            raise failures[0]
    finally:
        await gateway.aclose()

    status_counts: dict[str, int] = {}
    records = sorted(
        records_by_job.values(), key=lambda item: (str(item["model"]), str(item["case_id"]))
    )
    for record in records:
        status = str(record["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    manifest = {
        "schema_version": BENCHMARK_RUN_SCHEMA_VERSION,
        "created_at": utc_now(),
        "implementation": implementation_identity(),
        "job_plan": str(plan_path.relative_to(run_root)),
        "job_plan_sha256": plan_sha256,
        "benchmark": dataset.manifest.to_dict(),
        "models": models,
        "review_configs": review_configs,
        "runtime_provenance": runtime_provenance,
        "fixture_tool": args.fixture_tool,
        "selection": selection,
        "resolved_inputs": resolved_inputs,
        "scheduler": scheduler,
        "preparation": {
            "prepared": len(cases_to_prepare) - len(preparation_failures),
            "skipped_resumed": len(cases) - len(cases_to_prepare),
            "failed": len(preparation_failures),
            "failures": dict(sorted(preparation_failures.items())),
        },
        "status_counts": dict(sorted(status_counts.items())),
        "records": records,
    }
    manifest_path = run_root / "run_manifest.json"
    atomic_write_json(manifest_path, manifest)
    _print_json(
        {
            "run_dir": str(run_root),
            "manifest": str(manifest_path),
            "status_counts": manifest["status_counts"],
        }
    )
    failures = status_counts.get("failed", 0) + status_counts.get("partial", 0)
    return 1 if failures else 0


def _run_manifest_artifact_paths(
    args: argparse.Namespace, load_dataset: Any
) -> tuple[list[Path], dict[Path, dict[str, Any]]]:
    """Return the complete, checksum-bound artifact population for a run.

    A run manifest is the commit point for a benchmark experiment.  Exporting a
    glob of whatever JSON happens to remain in the directory can otherwise omit
    failed cases, include stale cases, or accept an artifact edited after the
    run.  Validate the full selected case/model matrix before returning paths.
    Each artifact's bytes are read exactly once — the checksum, the identity
    validation, and the returned parsed content all describe one snapshot, so
    a concurrent rewrite can never make the manifest certify bytes that were
    not checksummed.
    """

    root = args.run_dir.expanduser().resolve()
    manifest_path = root / "run_manifest.json"
    if not manifest_path.is_file():
        raise CliError("run directory has no run_manifest.json; use --artifacts for manual input")
    try:
        manifest = load_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read run manifest {manifest_path}: {exc}") from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != BENCHMARK_RUN_SCHEMA_VERSION
    ):
        raise CliError("run directory has an unsupported benchmark manifest")
    if manifest.get("implementation") != implementation_identity():
        raise CliError("run manifest was produced by a different BugBunny implementation")

    fixture_tool = manifest.get("fixture_tool")
    if not isinstance(fixture_tool, str) or not fixture_tool:
        raise CliError("run manifest has no fixture_tool")
    dataset = load_dataset(
        args.benchmark_data,
        preferred_fixture_tool=fixture_tool,
        require_preferred_tool=True,
        expected_case_count=50,
    )
    manifest_benchmark = manifest.get("benchmark")
    current_benchmark = dataset.manifest.to_dict()
    if not isinstance(manifest_benchmark, Mapping):
        raise CliError("run manifest has no benchmark identity")
    for field in (
        "schema_version",
        "benchmark_sha256",
        "golden_sha256",
        "case_count",
        "golden_issue_count",
        "preferred_fixture_tool",
        "fixture_tool_counts",
    ):
        if manifest_benchmark.get(field) != current_benchmark.get(field):
            raise CliError(f"run manifest benchmark {field} does not match --benchmark-data")

    models = manifest.get("models")
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(model, str) or not model for model in models)
        or len(set(models)) != len(models)
    ):
        raise CliError("run manifest models must be a non-empty unique string array")
    review_configs = manifest.get("review_configs")
    runtime_provenance = manifest.get("runtime_provenance")
    if not isinstance(review_configs, Mapping) or set(review_configs) != set(models):
        raise CliError("run manifest review_configs do not match its models")
    if not isinstance(runtime_provenance, Mapping) or set(runtime_provenance) != set(models):
        raise CliError("run manifest runtime_provenance does not match its models")

    selection = manifest.get("selection")
    if not isinstance(selection, Mapping):
        raise CliError("run manifest has no selection identity")
    selection_filter = selection.get("filter")
    selection_limit = selection.get("limit")
    selection_count = selection.get("case_count")
    if selection_filter is not None and not isinstance(selection_filter, str):
        raise CliError("run manifest selection.filter is invalid")
    if selection_limit is not None and (
        not isinstance(selection_limit, int)
        or isinstance(selection_limit, bool)
        or selection_limit <= 0
    ):
        raise CliError("run manifest selection.limit is invalid")
    if (
        not isinstance(selection_count, int)
        or isinstance(selection_count, bool)
        or selection_count <= 0
    ):
        raise CliError("run manifest selection.case_count is invalid")
    selected_cases = [case for case in dataset.cases if _matches_case(case, selection_filter)]
    if selection_limit is not None:
        selected_cases = selected_cases[:selection_limit]
    expected_case_ids = {case.case_id for case in selected_cases}
    if len(expected_case_ids) != selection_count:
        raise CliError("run manifest selection does not match the supplied benchmark")
    cases_by_id = {case.case_id: case for case in selected_cases}
    resolved_inputs = manifest.get("resolved_inputs")
    if not isinstance(resolved_inputs, Mapping) or set(resolved_inputs) != expected_case_ids:
        raise CliError("run manifest resolved_inputs do not match the selected cases")
    for case_id, case in cases_by_id.items():
        resolved = resolved_inputs.get(case_id)
        if not (
            isinstance(resolved, Mapping)
            and resolved.get("review_url") == case.review_url
            # Match resolve_pr's contract (40-hex SHA-1 or 64-hex SHA-256):
            # a stricter length here would make a completed paid run
            # permanently unexportable on a SHA-256 repository.
            and isinstance(resolved.get("base_sha"), str)
            and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", resolved["base_sha"])
            and isinstance(resolved.get("head_sha"), str)
            and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", resolved["head_sha"])
        ):
            raise CliError(f"run manifest has invalid resolved input for case {case_id}")

    records = manifest.get("records")
    expected_records = len(expected_case_ids) * len(models)
    if not isinstance(records, list) or len(records) != expected_records:
        raise CliError("run manifest does not contain one record for every selected case/model")

    seen_pairs: set[tuple[str, str]] = set()
    seen_paths: set[Path] = set()
    cases_by_model: dict[str, set[str]] = {model: set() for model in models}
    status_counts: dict[str, int] = {}
    paths: list[Path] = []
    artifacts_by_path: dict[Path, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise CliError(f"run manifest record {index} is not an object")
        model = record.get("model")
        case_id = record.get("case_id")
        status = record.get("status")
        if model not in cases_by_model or not isinstance(case_id, str):
            raise CliError(f"run manifest record {index} has an unknown model or case")
        if case_id not in expected_case_ids:
            raise CliError(f"run manifest record {index} is outside the selected cases")
        pair = (model, case_id)
        if pair in seen_pairs:
            raise CliError(f"run manifest duplicates case/model {case_id}/{model}")
        seen_pairs.add(pair)
        cases_by_model[model].add(case_id)
        if status not in {"completed", "resumed"}:
            raise CliError(f"run manifest case/model {case_id}/{model} is not complete: {status!r}")
        status_counts[status] = status_counts.get(status, 0) + 1

        relative_value = record.get("artifact")
        expected_sha256 = record.get("artifact_sha256")
        if not isinstance(relative_value, str) or not relative_value:
            raise CliError(f"run manifest record {index} has no artifact path")
        relative_path = Path(relative_value)
        if relative_path.is_absolute():
            raise CliError(f"run manifest record {index} has an absolute artifact path")
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise CliError(f"run manifest record {index} escapes the run directory") from exc
        if path in seen_paths:
            raise CliError(f"run manifest reuses artifact path {relative_value!r}")
        seen_paths.add(path)
        if not path.is_file():
            raise CliError(f"run artifact does not exist: {path}")
        if not (
            isinstance(expected_sha256, str)
            and len(expected_sha256) == 64
            and all(character in "0123456789abcdef" for character in expected_sha256)
        ):
            raise CliError(f"run manifest record {index} has an invalid artifact checksum")
        try:
            raw_artifact = path.read_bytes()
        except OSError as exc:
            raise CliError(f"cannot read artifact {path}: {exc}") from exc
        if sha256_bytes(raw_artifact) != expected_sha256:
            raise CliError(f"run artifact checksum does not match its manifest: {path}")

        try:
            artifact = json.loads(raw_artifact.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CliError(f"cannot read artifact {path}: {exc}") from exc
        artifact_config = artifact.get("config") if isinstance(artifact, Mapping) else None
        artifact_benchmark = artifact.get("benchmark") if isinstance(artifact, Mapping) else None
        if not (
            isinstance(artifact, Mapping)
            and artifact.get("status") == "completed"
            and isinstance(artifact_config, Mapping)
            and dict(artifact_config) == review_configs[model]
            and artifact_config.get("model") == model
            and isinstance(artifact.get("runtime"), Mapping)
            and dict(artifact["runtime"]) == runtime_provenance[model]
            and isinstance(artifact_benchmark, Mapping)
            and artifact_benchmark.get("case_id") == case_id
            and isinstance(artifact.get("pr"), Mapping)
            and artifact["pr"].get("url") == resolved_inputs[case_id]["review_url"]
            and artifact["pr"].get("base_sha") == resolved_inputs[case_id]["base_sha"]
            and artifact["pr"].get("head_sha") == resolved_inputs[case_id]["head_sha"]
        ):
            raise CliError(f"run artifact identity does not match its manifest: {path}")
        paths.append(path)
        artifacts_by_path[path] = artifact

    if any(case_ids != expected_case_ids for case_ids in cases_by_model.values()):
        raise CliError("run manifest has an incomplete selected case/model population")
    if manifest.get("status_counts") != dict(sorted(status_counts.items())):
        raise CliError("run manifest status_counts do not match its records")
    return paths, artifacts_by_path


def _artifact_paths(args: argparse.Namespace, *, load_dataset: Any | None = None) -> list[Path]:
    if args.artifacts:
        paths = [path.expanduser().resolve() for path in args.artifacts]
    else:
        if load_dataset is None:
            raise CliError("run-directory export requires benchmark dataset validation")
        paths, _artifacts = _run_manifest_artifact_paths(args, load_dataset)
    if not paths:
        raise CliError("no review artifacts were found")
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise CliError(f"artifact does not exist: {missing[0]}")
    return paths


def _export_artifacts(args: argparse.Namespace, *, load_dataset: Any) -> list[dict[str, Any]]:
    """Load export inputs, certifying exactly the bytes that were validated.

    The run-directory path holds the run-dir lock while reading, so a
    concurrent run/resume cannot swap an artifact between its checksum and
    the content this export writes into the shared bundle.
    """

    if args.artifacts:
        return _load_artifacts(_artifact_paths(args, load_dataset=load_dataset))
    with _run_dir_lock(args.run_dir.expanduser().resolve()):
        paths, artifacts_by_path = _run_manifest_artifact_paths(args, load_dataset)
    if not paths:
        raise CliError("no review artifacts were found")
    artifacts: list[dict[str, Any]] = []
    for path in paths:
        value = artifacts_by_path[path]
        if not isinstance(value, dict) or "findings" not in value or "pr" not in value:
            raise CliError(f"not a BugBunny review artifact: {path}")
        artifacts.append(value)
    return artifacts


def _load_artifacts(paths: Iterable[Path]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in paths:
        try:
            value = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CliError(f"cannot read artifact {path}: {exc}") from exc
        if not isinstance(value, dict) or "findings" not in value or "pr" not in value:
            raise CliError(f"not a BugBunny review artifact: {path}")
        artifacts.append(value)
    return artifacts


def _require_comparable_model_sweep(
    by_model: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Reject multi-model exports that did not review the same immutable inputs."""

    if len(by_model) < 2:
        return
    snapshots: dict[str, dict[str, tuple[tuple[str, str, str, str], str]]] = {}
    for model, artifacts in sorted(by_model.items()):
        model_snapshots: dict[str, tuple[tuple[str, str, str, str], str]] = {}
        for artifact in artifacts:
            benchmark = artifact.get("benchmark")
            pr = artifact.get("pr")
            diff = artifact.get("diff")
            case_id = benchmark.get("case_id") if isinstance(benchmark, Mapping) else None
            golden_url = benchmark.get("golden_url") if isinstance(benchmark, Mapping) else None
            review_url = benchmark.get("review_url") if isinstance(benchmark, Mapping) else None
            if isinstance(pr, Mapping) and not review_url:
                review_url = pr.get("url")
            base_sha = pr.get("base_sha") if isinstance(pr, Mapping) else None
            head_sha = pr.get("head_sha") if isinstance(pr, Mapping) else None
            diff_sha = diff.get("sha256") if isinstance(diff, Mapping) else None
            identity = (golden_url, review_url, base_sha, head_sha)
            if (
                not isinstance(case_id, str)
                or not case_id
                or any(not isinstance(value, str) or not value for value in identity)
                or not isinstance(diff_sha, str)
                or not diff_sha
            ):
                raise CliError(
                    "multi-model export requires case_id, golden/review URLs, base/head SHAs, "
                    "and diff.sha256 in every artifact"
                )
            if case_id in model_snapshots:
                raise CliError(f"model {model!r} duplicates benchmark case {case_id!r}")
            model_snapshots[case_id] = (identity, diff_sha)  # type: ignore[arg-type]
        snapshots[model] = model_snapshots

    baseline_model = sorted(snapshots)[0]
    baseline = snapshots[baseline_model]
    for model in sorted(snapshots):
        if set(snapshots[model]) != set(baseline):
            raise CliError(
                f"model sweep case population differs between {baseline_model!r} and {model!r}"
            )
        for case_id in sorted(baseline):
            # The diff hash is part of the immutable input identity; there is
            # deliberately no semantic-field fallback that would let differing
            # raw diffs pass as comparable.
            if snapshots[model][case_id] != baseline[case_id]:
                raise CliError(
                    f"model sweep fixture snapshot differs for case {case_id!r} between "
                    f"{baseline_model!r} and {model!r}"
                )


def _require_export_bundle_identity(output_root: Path) -> None:
    """Preflight current-schema export metadata before invoking an exporter.

    The production exporter repeats this check while holding its write lock.
    Keeping a CLI boundary check also prevents custom or test exporters from
    touching a bundle that already declares a different source build.
    """

    expected = implementation_identity()
    with file_lock(output_root / ".bugbunny-export.lock"):
        metadata = [
            *(
                (path, EXPORT_MANIFEST_SCHEMA_VERSION)
                for path in sorted(output_root.glob("*/*_export_manifest.json"))
            ),
            *(
                (path, EXPORT_INDEX_SCHEMA_VERSION)
                for path in sorted(output_root.glob("*/bugbunny_export_index.json"))
            ),
        ]
        for path, schema in metadata:
            try:
                value = load_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise CliError(f"cannot read existing export metadata {path}: {exc}") from exc
            if not isinstance(value, Mapping):
                raise CliError(f"existing export metadata is not an object: {path}")
            if path.name == "bugbunny_export_index.json":
                native = True
            else:
                actual_schema = value.get("schema_version")
                native = (
                    value.get("tool") == "bugbunny"
                    or str(value.get("tool_id") or "").startswith("bugbunny-")
                    or (
                        isinstance(actual_schema, str)
                        and actual_schema.startswith("bugbunny-codereviewbench-export-")
                    )
                )
            if not native:
                continue
            if value.get("schema_version") != schema:
                raise CliError(f"legacy BugBunny export metadata is unsupported: {path}")
            if value.get("implementation") != expected:
                raise CliError(f"export metadata belongs to another implementation: {path}")


def _benchmark_export(args: argparse.Namespace) -> int:
    load_dataset, export_results, sanitize_model_name, _artifact_model_directory = _benchmark_api()
    artifacts = _export_artifacts(args, load_dataset=load_dataset)
    by_model: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        config = artifact.get("config")
        model = config.get("model") if isinstance(config, Mapping) else None
        if not isinstance(model, str) or not model:
            raise CliError("every exported artifact must identify config.model")
        by_model.setdefault(model, []).append(artifact)
    _require_comparable_model_sweep(by_model)

    output_root = args.output_dir.expanduser().resolve()
    _require_export_bundle_identity(output_root)
    exports: list[dict[str, Any]] = []
    export_results_by_model: list[Any] = []
    finding_stages = tuple(dict.fromkeys(args.finding_stage or ["balanced"]))
    for model, model_artifacts in sorted(by_model.items()):
        for finding_stage in finding_stages:
            result = export_results(
                args.benchmark_data,
                model_artifacts,
                output_dir=args.output_dir,
                judge_model=args.judge_model,
                review_model=model,
                expected_case_count=50,
                finding_stage=finding_stage,
            )
            export_results_by_model.append(result)
            exports.append(
                {
                    "model": model,
                    "finding_stage": finding_stage,
                    "tool_id": result.tool_id,
                    "reviews": result.review_count,
                    "candidates": result.candidate_count,
                    "benchmark_data": str(result.benchmark_data_path.relative_to(output_root)),
                    "candidates_path": str(result.candidates_path.relative_to(output_root)),
                    "dedup_groups_path": str(result.dedup_groups_path.relative_to(output_root)),
                    "manifest": str(result.manifest_path.relative_to(output_root)),
                    "candidate_audit": (
                        str(result.candidate_audit_path.relative_to(output_root))
                        if getattr(result, "candidate_audit_path", None) is not None
                        else None
                    ),
                }
            )
    index_path = (
        args.output_dir.expanduser().resolve()
        / sanitize_model_name(args.judge_model)
        / "bugbunny_export_index.json"
    )
    # Exporters commit under this same bundle lock. Reacquire it after the
    # per-model calls and derive the index from every committed manifest instead
    # of overwriting it with only this invocation's tracks. This also closes the
    # window where another process can extend the bundle between the last export
    # and the index commit.
    with file_lock(output_root / ".bugbunny-export.lock"):
        final_manifest = load_json(export_results_by_model[-1].manifest_path)
        final_output_hashes = (
            dict(final_manifest.get("output_files_sha256", {}))
            if isinstance(final_manifest, Mapping)
            else {}
        )
        if not final_output_hashes:
            # Test doubles and older custom exporters may expose the committed
            # hashes only on their return value.
            final_output_hashes = dict(export_results_by_model[-1].output_files_sha256)

        merged: dict[str, dict[str, Any]] = {}
        for entry, result in zip(exports, export_results_by_model, strict=True):
            manifest = load_json(result.manifest_path)
            if not isinstance(manifest, Mapping) or manifest.get("output_files_sha256") != (
                final_output_hashes
            ):
                raise CliError("export manifests do not identify one final Step 3 bundle")
            entry["manifest_sha256"] = sha256_bytes(result.manifest_path.read_bytes())
            merged[str(entry["tool_id"])] = entry

        judge_dir = index_path.parent
        for manifest_path in sorted(judge_dir.glob("*_export_manifest.json")):
            manifest = load_json(manifest_path)
            if not isinstance(manifest, Mapping):
                raise CliError(f"export manifest is not an object: {manifest_path}")
            if manifest.get("schema_version") != EXPORT_MANIFEST_SCHEMA_VERSION:
                continue
            if manifest.get("implementation") != implementation_identity():
                raise CliError(
                    f"export manifest belongs to another implementation: {manifest_path}"
                )
            if manifest.get("judge_model") != args.judge_model:
                raise CliError(f"export manifest belongs to another judge: {manifest_path}")
            if manifest.get("output_files_sha256") != final_output_hashes:
                raise CliError("committed export manifests identify different Step 3 bundles")
            tool_id = str(manifest.get("tool_id") or "")
            model = str(manifest.get("review_model") or "")
            finding_stage = str(manifest.get("finding_stage") or "")
            audit_name = manifest.get("candidate_audit_file")
            if not tool_id or not model or not finding_stage or not isinstance(audit_name, str):
                raise CliError(f"export manifest lacks index identity: {manifest_path}")
            merged[tool_id] = {
                "model": model,
                "finding_stage": finding_stage,
                "tool_id": tool_id,
                "reviews": int(manifest.get("review_count") or 0),
                "candidates": int(manifest.get("candidate_count") or 0),
                "benchmark_data": str(
                    (output_root / "benchmark_data.json").relative_to(output_root)
                ),
                "candidates_path": str((judge_dir / "candidates.json").relative_to(output_root)),
                "dedup_groups_path": str(
                    (judge_dir / "dedup_groups.json").relative_to(output_root)
                ),
                "manifest": str(manifest_path.relative_to(output_root)),
                "candidate_audit": str((judge_dir / audit_name).relative_to(output_root)),
                "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
            }

        cumulative_exports = sorted(
            merged.values(),
            key=lambda entry: (
                str(entry.get("model") or ""),
                str(entry.get("finding_stage") or ""),
                str(entry.get("tool_id") or ""),
            ),
        )
        atomic_write_json(
            index_path,
            {
                "schema_version": EXPORT_INDEX_SCHEMA_VERSION,
                "created_at": utc_now(),
                "implementation": implementation_identity(),
                "judge_model": args.judge_model,
                "output_files_sha256": final_output_hashes,
                "exports": cumulative_exports,
            },
        )
    _print_json({"exports": cumulative_exports, "index": str(index_path)})
    return 0


def _benchmark_verify_export(args: argparse.Namespace) -> int:
    from bugbunny.benchmark import verify_codereviewbench_export_manifest

    manifest_path = Path(args.manifest).expanduser().resolve()
    results_root = manifest_path.parent.parent
    # Share the root export lock with export and judge: an unlocked verify
    # racing a concurrent export would report hashes for a bundle state that
    # never coexisted with the content it checked.
    with file_lock(results_root / ".bugbunny-export.lock"):
        report = verify_codereviewbench_export_manifest(manifest_path)
    _print_json(report)
    return 0


async def _calibrate(args: argparse.Namespace) -> int:
    from bugbunny.calibration import calibrate_verifier, verify_corpus_benchmark_disjoint

    if not 0 <= args.minimum_precision <= 1:
        raise CliError("--minimum-precision must be between 0 and 1")
    if args.benchmark_data is not None:
        verify_corpus_benchmark_disjoint(args.corpus, args.benchmark_data)
    gateway_config = _gateway_config(args, max_output_tokens=args.max_output_tokens)
    async with ModelGateway(gateway_config, max_concurrency=args.concurrency) as gateway:
        result = await calibrate_verifier(
            corpus_path=args.corpus,
            output_path=args.output,
            gateway=gateway,
            verifier_model=args.verifier_model,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
            concurrency=args.concurrency,
            minimum_precision=args.minimum_precision,
        )
    _print_json(
        {
            "operating_point": str(args.output.expanduser().resolve()),
            "operating_point_id": result["operating_point_id"],
            "threshold": result["threshold"],
            "selection": result["selection"]["selected"],
        }
    )
    return 0


async def _benchmark_judge(args: argparse.Namespace) -> int:
    from bugbunny.judge import run_codereviewbench_judge

    credential = GatewayConfig(
        api_key=args.api_key,
        api_key_env=args.api_key_env,
        api_base=args.api_base,
        dotenv_path=args.env_file,
    )
    api_key = credential.resolved_api_key()
    # A dotenv-sourced key is in neither argv nor the environment, so the
    # redaction boundary in main() cannot recover it on its own; register it
    # like every other credential-resolving command does via _gateway_config.
    _register_runtime_secret(api_key)
    _register_runtime_secret(credential.api_base)
    if api_key is None:
        raise CliError(
            "Martian API key is not configured; set MARTIAN_API_KEY, add it to .env, "
            "or pass --api-key-env"
        )
    report = await run_codereviewbench_judge(
        results_dir=args.results_dir,
        judge_model=args.judge_model,
        api_key=api_key,
        api_base=credential.effective_api_base(),
        tools=args.tool,
        judge_concurrency=args.judge_concurrency,
        review_concurrency=args.review_concurrency,
        call_timeout_seconds=args.call_timeout,
        review_timeout_seconds=args.review_timeout,
        max_attempts=args.max_retries,
        force=args.force,
        evaluations_file=args.evaluations_file,
    )
    _print_json(report)
    errors = sum(metric["errors"] for metric in report["metrics"].values())
    return 1 if report["timed_out"] or errors else 0


def _benchmark_analyze(args: argparse.Namespace) -> int:
    from bugbunny.analysis import analyze_evaluation, render_analysis_markdown
    from bugbunny.benchmark import sanitize_model_name
    from bugbunny.util import atomic_write_text

    results_root = args.results_dir.expanduser().resolve()
    judge_dir = results_root / sanitize_model_name(args.judge_model)
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json is not None
        else judge_dir / "bugbunny_evaluation_audit.json"
    )
    # Hold the root export lock for the whole analysis: the report claims to
    # describe one bundle state, so export and judge must not rewrite the
    # shared Step 3 files while it is being computed.
    with file_lock(results_root / ".bugbunny-export.lock"):
        report = analyze_evaluation(
            run_dir=args.run_dir,
            results_dir=args.results_dir,
            judge_model=args.judge_model,
            output_json=output_json,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
            allow_judge_errors=args.allow_judge_errors,
        )
    markdown_path = output_json.with_suffix(".md")
    atomic_write_text(markdown_path, render_analysis_markdown(report))
    _print_json({"audit_json": str(output_json), "audit_markdown": str(markdown_path)})
    return 0


def _publish(args: argparse.Namespace) -> int:
    if not (args.confirm_publish or args.yes):
        raise CliError("publishing writes to GitHub; pass --confirm-publish or --yes")
    artifact = _load_artifacts([args.artifact.expanduser().resolve()])[0]
    pr_value = artifact.get("pr")
    if not isinstance(pr_value, Mapping) or not isinstance(pr_value.get("url"), str):
        raise CliError("artifact does not contain a pull-request URL")
    token = None
    if args.github_token_env:
        token = os.environ.get(args.github_token_env)
        if not token:
            raise CliError(f"GitHub token environment variable is not set: {args.github_token_env}")
    GitHubClient, GitHubReviewPublisher = _github_types()
    with GitHubClient(token=token) as github:
        pr = github.resolve_pr(pr_value["url"])
        result = GitHubReviewPublisher(github).publish(
            pr,
            artifact,
            publish_clean=args.publish_clean,
        )
    _print_json(_safe_mapping(result))
    # ``clean_not_published`` is a declined action, not a success: nothing was
    # written and the operator did not pass --publish-clean. A script chaining
    # on exit status must be able to tell it from ``published``/
    # ``already_published``, which both mean the review exists on GitHub.
    return 1 if getattr(result, "status", None) == "clean_not_published" else 0


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "calibrate":
        return await _calibrate(args)
    if args.command == "review-pr":
        return await _review_pr(args)
    if args.command == "benchmark" and args.benchmark_command == "run":
        return await _benchmark_run(args)
    if args.command == "benchmark" and args.benchmark_command == "export":
        return _benchmark_export(args)
    if args.command == "benchmark" and args.benchmark_command == "verify-export":
        return _benchmark_verify_export(args)
    if args.command == "benchmark" and args.benchmark_command == "judge":
        return await _benchmark_judge(args)
    if args.command == "benchmark" and args.benchmark_command == "analyze":
        return _benchmark_analyze(args)
    if args.command == "publish":
        return _publish(args)
    raise CliError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        return asyncio.run(async_main(arguments))
    except KeyboardInterrupt:
        print("BugBunny interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        # A single redacting boundary: any escaping failure class would
        # otherwise print an unredacted traceback and skip the exit contract.
        rendered = (
            str(exc)
            if isinstance(exc, (RuntimeError, FileNotFoundError, ValueError))
            else f"{type(exc).__name__}: {exc}"
        )
        message = _redact_text(rendered, _argument_secrets(arguments))
        print(f"bugbunny: {message}", file=sys.stderr)
        return 2


__all__ = ["CliError", "async_main", "build_parser", "main"]
