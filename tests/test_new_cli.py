from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from bugbunny import cli
from bugbunny.build import (
    BENCHMARK_RUN_SCHEMA_VERSION,
    EXPORT_MANIFEST_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    implementation_identity,
)


class _Client:
    resolved: ClassVar[list[str]] = []

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def resolve_pr(self, url: str) -> cli.PRInfo:
        self.resolved.append(url)
        return cli.PRInfo(
            url=url,
            owner="fixture",
            repo="repo",
            number=1,
            clone_url="https://github.com/fixture/repo.git",
            title="fixture",
            body="",
            base_ref="main",
            base_sha="b" * 40,
            head_ref="change",
            head_sha="a" * 40,
            resolved_at="2026-01-01T00:00:00Z",
        )


class _Cache:
    def __init__(self, path: Path, **_kwargs: Any) -> None:
        self.path = path

    def prepare(self, _pr: Any) -> SimpleNamespace:
        return SimpleNamespace(diff_bytes=100)


@dataclass
class _Artifact:
    model: str
    status: str = "completed"
    run_id: str = "run-1"
    benchmark: dict[str, Any] | None = None

    @property
    def findings(self) -> list[Any]:
        return []

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "implementation": implementation_identity(),
            "status": self.status,
            "run_id": self.run_id,
            "config": {"model": self.model},
            "pr": {"url": "https://github.com/fixture/repo/pull/1"},
            "findings": [],
            "benchmark": self.benchmark,
        }


class _Engine:
    instances: ClassVar[list[_Engine]] = []

    def __init__(self, config: Any, gateway: Any, repository_cache: Any) -> None:
        self.config = config
        self.gateway = gateway
        self.repository_cache = repository_cache
        self.__class__.instances.append(self)

    async def review(self, _pr: Any) -> _Artifact:
        return _Artifact(self.config.model)


def _writer(
    artifact: _Artifact,
    output_path: Path,
    *,
    markdown_path: Path | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
    if markdown_path:
        markdown_path.write_text("# review\n", encoding="utf-8")
    return output_path


def test_config_defaults_and_does_not_serialize_credentials() -> None:
    balanced = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--api-key",
            "do-not-persist",
        ]
    )
    review_config = cli._review_config(balanced)
    gateway_config = cli._gateway_config(balanced)

    assert review_config.verifier_model == "same"
    assert review_config.verification_semantic_retries == 2
    assert "do-not-persist" not in json.dumps(review_config.to_dict())
    assert gateway_config.resolved_api_key() == "do-not-persist"

    fast = cli.build_parser().parse_args(
        ["review-pr", "https://github.com/o/r/pull/1", "--profile", "fast"]
    )
    assert cli._review_config(fast).verifier_model is None

    no_semantic_retry = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--verification-semantic-retries",
            "0",
        ]
    )
    assert cli._review_config(no_semantic_retry).verification_semantic_retries == 0


def test_custom_api_key_environment_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CUSTOM_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("MARTIAN_API_KEY", "default-secret")
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--api-key-env",
            "CUSTOM_PROVIDER_API_KEY",
        ]
    )

    assert cli._gateway_config(args).resolved_api_key() == "default-secret"


def test_fast_profile_rejects_explicit_verifier() -> None:
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--profile",
            "fast",
            "--verifier-model",
            "openai/a-model",
        ]
    )
    with pytest.raises(cli.CliError, match="cannot be combined"):
        cli._review_config(args)


def test_context_defaults_are_generous_and_every_bound_is_configurable() -> None:
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--context-mode",
            "agentic",
            "--max-chunk-chars",
            "90000",
            "--max-context-chars",
            "150000",
            "--initial-context-chars",
            "40000",
            "--context-selection-rounds",
            "1",
            "--context-requests-per-round",
            "7",
            "--max-context-files",
            "20",
            "--context-read-lines",
            "300",
            "--context-read-chars",
            "30000",
            "--context-search-hits",
            "18",
            "--repository-index-chars",
            "70000",
        ]
    )
    config = cli._review_config(args)

    assert cli.ReviewConfig().max_context_chars == 120_000
    assert cli.ReviewConfig().max_chunk_chars == 72_000
    assert config.context_mode == "agentic"
    assert config.max_chunk_chars == 90_000
    assert config.max_context_chars == 150_000
    assert config.initial_context_chars == 40_000
    assert config.context_selection_rounds == 1
    assert config.context_requests_per_round == 7
    assert config.max_context_files == 20
    assert config.context_read_lines == 300
    assert config.context_read_chars == 30_000
    assert config.context_search_hits == 18
    assert config.repository_index_chars == 70_000


def test_declared_model_windows_derive_distinct_frozen_sweep_budgets() -> None:
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--benchmark-data",
            "benchmark_data.json",
            "--model",
            "provider/small",
            "--model",
            "provider/large",
            "--context-window-tokens",
            "32768",
            "--model-context-window",
            "provider/large=200000",
        ]
    )
    cli._validate_context_window_models(args, args.model)
    small = cli._review_config(args, model="provider/small")
    large = cli._review_config(args, model="provider/large")

    assert small.context_budget_source == large.context_budget_source == "declared_window"
    assert small.context_window_tokens == 32_768
    assert large.context_window_tokens == 200_000
    assert small.generation_input_char_budget == 40_960
    assert large.generation_input_char_budget == 326_272
    assert small.max_chunk_chars + small.max_context_chars == 28_960
    assert large.max_chunk_chars + large.max_context_chars == 314_272
    assert small.verifier_input_char_budget == small.generation_input_char_budget
    assert large.verifier_input_char_budget == large.generation_input_char_budget
    assert large.max_context_chars > small.max_context_chars
    assert large.max_context_files > small.max_context_files
    assert large.repository_index_chars > small.repository_index_chars
    small.validate()
    large.validate()


def test_agentic_declared_window_reserves_selection_headroom_at_32k() -> None:
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--profile",
            "fast",
            "--context-mode",
            "agentic",
            "--context-window-tokens",
            "32768",
        ]
    )

    config = cli._review_config(args)

    assert config.initial_context_chars == 5_792
    assert config.initial_context_chars < config.max_context_chars
    assert config.max_context_chars - config.initial_context_chars == 11_584


def test_pinned_verifier_needs_its_own_window_in_an_adaptive_run() -> None:
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--context-window-tokens",
            "200000",
            "--verifier-model",
            "provider/verifier",
        ]
    )
    with pytest.raises(cli.CliError, match="pinned verifier"):
        cli._review_config(args)

    declared = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--context-window-tokens",
            "200000",
            "--verifier-model",
            "provider/verifier",
            "--verifier-context-window-tokens",
            "64000",
        ]
    )
    config = cli._review_config(declared)
    assert config.verifier_context_window_tokens == 64_000
    assert config.verifier_input_char_budget == 87_808
    assert config.verifier_max_output_tokens == 16_000


def test_generation_and_verifier_can_use_separate_completion_caps() -> None:
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/1",
            "--context-window-tokens",
            "200000",
            "--verifier-model",
            "provider/verifier",
            "--verifier-context-window-tokens",
            "200000",
            "--max-output-tokens",
            "50000",
            "--verifier-max-output-tokens",
            "32768",
        ]
    )

    config = cli._review_config(args)

    assert config.max_output_tokens == 50_000
    assert config.verifier_max_output_tokens == 32_768
    assert config.generation_input_char_budget == 291_808
    assert config.verifier_input_char_budget == 326_272


def test_model_window_override_must_name_a_selected_model() -> None:
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--benchmark-data",
            "benchmark_data.json",
            "--model",
            "provider/selected",
            "--model-context-window",
            "provider/other=200000",
        ]
    )
    with pytest.raises(cli.CliError, match="unselected model"):
        cli._validate_context_window_models(args, args.model)


def test_resume_requires_exact_dataset_snapshot_config_and_prompt_identity(
    tmp_path: Path,
) -> None:
    from bugbunny import __version__
    from bugbunny.exploration import exploration_prompt_sha256, exploration_schema_sha256
    from bugbunny.prompts import generation_prompt_sha256, verifier_prompt_sha256

    config = cli._review_config(
        cli.build_parser().parse_args(
            ["review-pr", "https://github.com/o/r/pull/1", "--profile", "fast"]
        )
    )
    path = tmp_path / "review.json"
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "tool_version": __version__,
        "implementation": implementation_identity(),
        "status": "completed",
        "config": config.to_dict(),
        "pr": {
            "url": "https://github.com/fixture/repo/pull/1",
            "base_sha": "b" * 40,
            "head_sha": "d" * 40,
        },
        "context": {
            "generation_prompt_sha256": generation_prompt_sha256(),
            "verifier_prompt_sha256": verifier_prompt_sha256(),
            "context_selection_prompt_sha256": exploration_prompt_sha256(),
            "context_selection_schema_sha256": exploration_schema_sha256(
                config.context_requests_per_round,
                config.context_search_max_offset,
            ),
        },
        "runtime": {"requested_model": config.model, "transport": "test"},
        "coverage": {"complete": True},
        "diff": {"chunk_plan_complete": True, "commentable_ranges": {}},
        "benchmark": {
            "case_id": "case-1",
            "review_url": "https://github.com/fixture/repo/pull/1",
            "golden_sha256": "c" * 64,
            "benchmark_sha256": "d" * 64,
            "dataset_golden_sha256": "e" * 64,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    expected = {
        "config": config,
        "case_id": "case-1",
        "review_url": "https://github.com/fixture/repo/pull/1",
        "golden_sha256": "c" * 64,
        "benchmark_sha256": "d" * 64,
        "dataset_golden_sha256": "e" * 64,
        "base_sha": "b" * 40,
        "head_sha": "d" * 40,
        "runtime": {"requested_model": config.model, "transport": "test"},
        "expected_sha256": cli.sha256_bytes(path.read_bytes()),
    }

    assert cli._completed_artifact(path, **expected)
    assert not cli._completed_artifact(path, **{**expected, "head_sha": "x" * 40})
    assert not cli._completed_artifact(path, **{**expected, "benchmark_sha256": "x" * 64})
    payload["implementation"] = {
        **implementation_identity(),
        "source_sha256": "0" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not cli._completed_artifact(
        path,
        **{
            **expected,
            "expected_sha256": cli.sha256_bytes(path.read_bytes()),
        },
    )
    payload["implementation"] = implementation_identity()
    payload["findings"] = [{"title": "tampered"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not cli._completed_artifact(path, **expected)


def test_run_dir_export_requires_complete_checksum_bound_population(tmp_path: Path) -> None:
    case = SimpleNamespace(
        case_id="case-1",
        repository="upstream/repo",
        golden_url="https://github.com/upstream/repo/pull/1",
        review_url="https://github.com/fixture/repo/pull/1",
        fixture_repo_name="fixture-repo",
    )
    benchmark_identity = {
        "schema_version": "bugbunny-codereviewbench-dataset-v1",
        "benchmark_data_path": "benchmark_data.json",
        "benchmark_sha256": "b" * 64,
        "golden_sha256": "g" * 64,
        "case_count": 50,
        "golden_issue_count": 173,
        "preferred_fixture_tool": "auto",
        "fixture_tool_counts": {"primarytool": 50},
    }
    dataset = SimpleNamespace(
        cases=(case,),
        manifest=SimpleNamespace(to_dict=lambda: benchmark_identity),
    )

    def load_dataset(*_args: Any, **kwargs: Any) -> Any:
        assert kwargs == {
            "preferred_fixture_tool": "auto",
            "require_preferred_tool": True,
            "expected_case_count": 50,
        }
        return dataset

    run_dir = tmp_path / "run"
    artifact_path = run_dir / "artifacts" / "provider_a" / "case-1.json"
    artifact_path.parent.mkdir(parents=True)
    config = {"model": "provider/a", "profile": "fast"}
    runtime = {
        "generation": {"requested_model": "provider/a", "transport": "test"},
        "verification": None,
    }
    artifact = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "implementation": implementation_identity(),
        "status": "completed",
        "config": config,
        "runtime": runtime,
        "benchmark": {"case_id": "case-1"},
        "pr": {
            "url": case.review_url,
            "base_sha": "b" * 40,
            "head_sha": "d" * 40,
        },
        "findings": [],
    }
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    record = {
        "case_id": "case-1",
        "model": "provider/a",
        "status": "completed",
        "artifact": str(artifact_path.relative_to(run_dir)),
        "artifact_sha256": cli.sha256_bytes(artifact_path.read_bytes()),
    }
    manifest = {
        "schema_version": BENCHMARK_RUN_SCHEMA_VERSION,
        "implementation": implementation_identity(),
        "benchmark": benchmark_identity,
        "fixture_tool": "auto",
        "models": ["provider/a"],
        "review_configs": {"provider/a": config},
        "runtime_provenance": {"provider/a": runtime},
        "selection": {"filter": None, "limit": 1, "case_count": 1},
        "resolved_inputs": {
            "case-1": {
                "review_url": case.review_url,
                "base_sha": "b" * 40,
                "head_sha": "d" * 40,
            }
        },
        "status_counts": {"completed": 1},
        "records": [record],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "export",
            "--benchmark-data",
            str(tmp_path / "benchmark_data.json"),
            "--run-dir",
            str(run_dir),
            "--judge-model",
            "judge/model",
            "--output-dir",
            str(tmp_path / "export"),
        ]
    )

    assert cli._artifact_paths(args, load_dataset=load_dataset) == [artifact_path.resolve()]

    artifact["findings"] = [{"title": "edited after run"}]
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(cli.CliError, match="checksum does not match"):
        cli._artifact_paths(args, load_dataset=load_dataset)

    artifact_path.write_text(json.dumps({**artifact, "findings": []}), encoding="utf-8")
    manifest["records"] = [{"case_id": "case-1", "model": "provider/a", "status": "failed"}]
    manifest["status_counts"] = {"failed": 1}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(cli.CliError, match="is not complete"):
        cli._artifact_paths(args, load_dataset=load_dataset)

    manifest["records"] = []
    manifest["status_counts"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(cli.CliError, match="one record for every selected case/model"):
        cli._artifact_paths(args, load_dataset=load_dataset)


def test_doctor_reports_presence_but_never_environment_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-secret-that-must-not-print"
    monkeypatch.setenv("MARTIAN_API_KEY", secret)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="Logged in with a value that is intentionally ignored\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda _name: "1.2.3")
    args = cli.build_parser().parse_args(["doctor"])

    assert cli._doctor(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert secret not in output
    assert report["environment"]["MARTIAN_API_KEY"] == {"present": True}
    assert report["martian"]["credential_configured"] is True
    assert report["codex"]["logged_in"] is True


@pytest.mark.asyncio
async def test_review_pr_runs_engine_and_writes_both_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Engine.instances.clear()
    _Client.resolved.clear()
    monkeypatch.setattr(cli, "_engine_types", lambda: (_Engine, _writer))
    monkeypatch.setattr(cli, "_repository_type", lambda: _Cache)
    monkeypatch.setattr(cli, "_github_types", lambda: (_Client, object))
    output = tmp_path / "review.json"
    markdown = tmp_path / "review.md"
    args = cli.build_parser().parse_args(
        [
            "review-pr",
            "https://github.com/o/r/pull/9",
            "--model",
            "codex/gpt-5.6-luna",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )

    assert await cli._review_pr(args) == 0
    assert output.is_file()
    assert markdown.is_file()
    assert _Client.resolved == ["https://github.com/o/r/pull/9"]
    assert _Engine.instances[0].config.model == "codex/gpt-5.6-luna"
    assert json.loads(capsys.readouterr().out)["findings"] == 0


@pytest.mark.asyncio
async def test_benchmark_run_filters_limits_and_creates_resumable_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Engine.instances.clear()
    _Client.resolved.clear()
    case_a = SimpleNamespace(
        case_id="alpha",
        repository="upstream/alpha",
        golden_url="https://github.com/upstream/alpha/pull/10",
        review_url="https://github.com/fixture/alpha/pull/1",
        fixture_repo_name="fixture-alpha",
        fixture_tool="primarytool",
        golden_sha256="1" * 64,
    )
    case_b = SimpleNamespace(
        case_id="beta",
        repository="upstream/beta",
        golden_url="https://github.com/upstream/beta/pull/20",
        review_url="https://github.com/fixture/beta/pull/1",
        fixture_repo_name="fixture-beta",
        fixture_tool="primarytool",
        golden_sha256="2" * 64,
    )
    manifest = SimpleNamespace(
        benchmark_sha256="b" * 64,
        golden_sha256="g" * 64,
        to_dict=lambda: {
            "case_count": 2,
            "benchmark_sha256": "b" * 64,
            "golden_sha256": "g" * 64,
        },
    )
    dataset = SimpleNamespace(cases=(case_a, case_b), manifest=manifest)

    def load_dataset(*_args: Any, **_kwargs: Any) -> Any:
        return dataset

    monkeypatch.setattr(
        cli,
        "_benchmark_api",
        lambda: (
            load_dataset,
            object(),
            lambda model: model.replace("/", "_"),
            lambda model: model.replace("/", "_"),
        ),
    )
    monkeypatch.setattr(cli, "_engine_types", lambda: (_Engine, _writer))
    monkeypatch.setattr(cli, "_repository_type", lambda: _Cache)
    monkeypatch.setattr(cli, "_github_types", lambda: (_Client, object))
    run_dir = tmp_path / "run"
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--benchmark-data",
            str(tmp_path / "benchmark_data.json"),
            "--model",
            "codex/a",
            "--model",
            "codex/b",
            "--filter",
            "alpha",
            "--limit",
            "1",
            "--run-dir",
            str(run_dir),
        ]
    )

    assert await cli._benchmark_run(args) == 0
    artifact_path = run_dir / "artifacts" / "codex_a" / "alpha.json"
    second_artifact_path = run_dir / "artifacts" / "codex_b" / "alpha.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["benchmark"]["golden_url"] == case_a.golden_url
    assert second_artifact_path.is_file()
    run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["selection"]["case_count"] == 1
    assert run_manifest["resolved_inputs"] == {
        "alpha": {
            "review_url": case_a.review_url,
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
        }
    }
    assert run_manifest["status_counts"] == {"completed": 2}
    assert _Client.resolved == [case_a.review_url]
    assert json.loads(capsys.readouterr().out)["status_counts"] == {"completed": 2}

    def advanced_fixture(self: _Client, url: str) -> cli.PRInfo:
        self.resolved.append(url)
        original = _Client.resolve_pr(self, url)
        return cli.PRInfo.from_dict({**original.to_dict(), "head_sha": "c" * 40})

    monkeypatch.setattr(_Client, "resolve_pr", advanced_fixture)
    assert await cli._benchmark_run(args) == 0
    assert _Client.resolved == [case_a.review_url]
    assert (run_dir / "job_plan.json").is_file()
    assert (run_dir / "run_checkpoint.json").is_file()
    assert len(_Engine.instances) == 4


@pytest.mark.asyncio
async def test_benchmark_scheduler_starts_largest_model_jobs_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cases = tuple(
        SimpleNamespace(
            case_id=name,
            repository=f"upstream/{name}",
            golden_url=f"https://github.com/upstream/{name}/pull/10",
            review_url=f"https://github.com/fixture/{name}/pull/1",
            fixture_repo_name=f"fixture-{name}",
            fixture_tool="primarytool",
            golden_sha256=character * 64,
        )
        for name, character in (("small", "1"), ("large", "2"))
    )
    dataset = SimpleNamespace(
        cases=cases,
        manifest=SimpleNamespace(
            benchmark_sha256="b" * 64,
            golden_sha256="g" * 64,
            to_dict=lambda: {
                "case_count": 2,
                "benchmark_sha256": "b" * 64,
                "golden_sha256": "g" * 64,
            },
        ),
    )

    class Client(_Client):
        def resolve_pr(self, url: str) -> cli.PRInfo:
            name = "large" if "/large/" in url else "small"
            return cli.PRInfo(
                url=url,
                owner="fixture",
                repo=name,
                number=1,
                clone_url=f"https://github.com/fixture/{name}.git",
                title=name,
                body="",
                base_ref="main",
                base_sha="b" * 40,
                head_ref="change",
                head_sha=("a" if name == "large" else "c") * 40,
                resolved_at="2026-01-01T00:00:00Z",
            )

    class Cache(_Cache):
        def prepare(self, pr: cli.PRInfo) -> SimpleNamespace:
            return SimpleNamespace(diff_bytes=10_000 if pr.repo == "large" else 10)

    class Engine(_Engine):
        starts: ClassVar[list[tuple[str, str]]] = []
        active: ClassVar[int] = 0
        maximum: ClassVar[int] = 0

        async def review(self, pr: cli.PRInfo) -> _Artifact:
            self.__class__.starts.append((self.config.model, pr.repo))
            self.__class__.active += 1
            self.__class__.maximum = max(self.__class__.maximum, self.__class__.active)
            await asyncio.sleep(0.01)
            self.__class__.active -= 1
            return _Artifact(self.config.model)

    monkeypatch.setattr(
        cli,
        "_benchmark_api",
        lambda: (
            lambda *_args, **_kwargs: dataset,
            object(),
            lambda model: model.replace("/", "_"),
            lambda model: model.replace("/", "_"),
        ),
    )
    monkeypatch.setattr(cli, "_engine_types", lambda: (Engine, _writer))
    monkeypatch.setattr(cli, "_repository_type", lambda: Cache)
    monkeypatch.setattr(cli, "_github_types", lambda: (Client, object))
    run_dir = tmp_path / "run"
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--benchmark-data",
            str(tmp_path / "benchmark_data.json"),
            "--model",
            "codex/a",
            "--model",
            "codex/b",
            "--active-reviews",
            "2",
            "--run-dir",
            str(run_dir),
        ]
    )

    assert await cli._benchmark_run(args) == 0
    assert Engine.starts[:2] == [("codex/a", "large"), ("codex/b", "large")]
    assert Engine.maximum == 2
    plan = json.loads((run_dir / "job_plan.json").read_text(encoding="utf-8"))
    assert set(plan["resolved_prs"]) == {"large", "small"}
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["scheduler"]["ordering"] == "largest-prepared-diff-first"
    assert manifest["scheduler"]["models_concurrent"] is True


def test_publish_requires_explicit_confirmation_before_loading_artifact(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    args = cli.build_parser().parse_args(["publish", str(missing)])
    with pytest.raises(cli.CliError, match="pass --confirm-publish or --yes"):
        cli._publish(args)


def test_main_turns_user_errors_into_exit_code_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["publish", "missing.json"]) == 2
    assert "--confirm-publish" in capsys.readouterr().err


def test_benchmark_export_groups_model_sweeps_and_preserves_previous_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run" / "artifacts"
    first = run_dir / "a" / "one.json"
    second = run_dir / "b" / "two.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    for path, model in ((first, "provider/z"), (second, "provider/a")):
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "config": {"model": model},
                    "benchmark": {
                        "case_id": "case-1",
                        "golden_url": "https://github.com/upstream/repo/pull/8",
                        "review_url": "https://github.com/fixture/repo/pull/1",
                    },
                    "pr": {
                        "url": "https://github.com/fixture/repo/pull/1",
                        "base_sha": "b" * 40,
                        "head_sha": "d" * 40,
                    },
                    "diff": {"sha256": "d" * 64},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )

    calls: list[tuple[Path, str]] = []

    def export_results(base: Path, _artifacts: Any, **kwargs: Any) -> Any:
        calls.append((Path(base), kwargs["review_model"]))
        output = Path(kwargs["output_dir"])
        judge = output / kwargs["judge_model"].replace("/", "_")
        judge.mkdir(parents=True, exist_ok=True)
        benchmark_output = output / "benchmark_data.json"
        candidates_output = judge / "candidates.json"
        groups_output = judge / "dedup_groups.json"
        for target in (benchmark_output, candidates_output, groups_output):
            target.write_text("{}\n", encoding="utf-8")
        output_hashes = {
            "benchmark_data.json": cli.sha256_bytes(benchmark_output.read_bytes()),
            "judge/candidates.json": cli.sha256_bytes(candidates_output.read_bytes()),
            "judge/dedup_groups.json": cli.sha256_bytes(groups_output.read_bytes()),
        }
        tool_id = f"bugbunny-{kwargs['review_model'].replace('/', '_')}"
        audit_name = f"{tool_id}_candidate_audit.json"
        (judge / audit_name).write_text("{}\n", encoding="utf-8")
        manifest_path = judge / f"{tool_id}_export_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": EXPORT_MANIFEST_SCHEMA_VERSION,
                    "implementation": implementation_identity(),
                    "judge_model": kwargs["judge_model"],
                    "review_model": kwargs["review_model"],
                    "finding_stage": kwargs["finding_stage"],
                    "tool_id": tool_id,
                    "review_count": 1,
                    "candidate_count": 0,
                    "candidate_audit_file": audit_name,
                    "output_files_sha256": output_hashes,
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            tool_id=tool_id,
            review_count=1,
            candidate_count=0,
            benchmark_data_path=benchmark_output,
            candidates_path=candidates_output,
            dedup_groups_path=groups_output,
            manifest_path=manifest_path,
            output_files_sha256=output_hashes,
        )

    monkeypatch.setattr(
        cli,
        "_benchmark_api",
        lambda: (
            object(),
            export_results,
            lambda value: value.replace("/", "_"),
            lambda value: value.replace("/", "_"),
        ),
    )
    output = tmp_path / "export"
    source = tmp_path / "benchmark_data.json"
    first_args = cli.build_parser().parse_args(
        [
            "benchmark",
            "export",
            "--benchmark-data",
            str(source),
            "--artifacts",
            str(first),
            "--judge-model",
            "judge/model",
            "--output-dir",
            str(output),
        ]
    )

    assert cli._benchmark_export(first_args) == 0
    first_report = json.loads(capsys.readouterr().out)
    assert len(first_report["exports"]) == 1

    second_args = cli.build_parser().parse_args(
        [
            "benchmark",
            "export",
            "--benchmark-data",
            str(source),
            "--artifacts",
            str(second),
            "--judge-model",
            "judge/model",
            "--output-dir",
            str(output),
        ]
    )
    assert cli._benchmark_export(second_args) == 0
    assert [model for _base, model in calls] == ["provider/z", "provider/a"]
    assert calls[0][0] == source
    assert calls[1][0] == source
    report = json.loads(capsys.readouterr().out)
    assert len(report["exports"]) == 2
    assert all(len(item["manifest_sha256"]) == 64 for item in report["exports"])
    assert Path(report["index"]).is_file()
    index = json.loads(Path(report["index"]).read_text(encoding="utf-8"))
    assert index["output_files_sha256"] == {
        "benchmark_data.json": cli.sha256_bytes((output / "benchmark_data.json").read_bytes()),
        "judge/candidates.json": cli.sha256_bytes(
            (output / "judge_model" / "candidates.json").read_bytes()
        ),
        "judge/dedup_groups.json": cli.sha256_bytes(
            (output / "judge_model" / "dedup_groups.json").read_bytes()
        ),
    }


def test_benchmark_export_rejects_incomparable_model_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths: list[Path] = []
    for index, (model, head_sha) in enumerate((("provider/a", "a" * 40), ("provider/b", "b" * 40))):
        path = tmp_path / f"artifact-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "config": {"model": model},
                    "benchmark": {
                        "case_id": "case-1",
                        "golden_url": "https://github.com/upstream/repo/pull/8",
                        "review_url": "https://github.com/fixture/repo/pull/1",
                    },
                    "pr": {
                        "url": "https://github.com/fixture/repo/pull/1",
                        "base_sha": "c" * 40,
                        "head_sha": head_sha,
                    },
                    "diff": {"sha256": "d" * 64},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    monkeypatch.setattr(
        cli,
        "_benchmark_api",
        lambda: (object(), object(), lambda value: value, lambda value: value),
    )
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "export",
            "--benchmark-data",
            str(tmp_path / "benchmark_data.json"),
            "--artifacts",
            *(str(path) for path in paths),
            "--judge-model",
            "judge/model",
            "--output-dir",
            str(tmp_path / "export"),
        ]
    )

    with pytest.raises(cli.CliError, match="fixture snapshot differs"):
        cli._benchmark_export(args)


def test_export_bundle_preflight_rejects_legacy_index(tmp_path: Path) -> None:
    output = tmp_path / "results"
    judge_dir = output / "judge_model"
    judge_dir.mkdir(parents=True)
    index_path = judge_dir / "bugbunny_export_index.json"
    index_path.write_text(
        json.dumps({"schema_version": "bugbunny-codereviewbench-export-index-v2"}),
        encoding="utf-8",
    )

    with pytest.raises(cli.CliError, match="legacy BugBunny export metadata"):
        cli._require_export_bundle_identity(output)


def test_model_sweep_rejects_any_diff_hash_divergence() -> None:
    # The diff hash is part of the immutable input identity. The former
    # "semantic fields" fallback let two models with different raw diffs pass
    # as comparable; identical metadata must no longer excuse a hash mismatch.
    semantic_diff = {
        "merge_base_sha": "c" * 40,
        "additions": 2,
        "deletions": 1,
        "files": 1,
        "hunks": 1,
        "commentable_ranges": {"RIGHT": {"src/example.py": [[1, 3]]}},
    }
    artifacts: dict[str, list[dict[str, Any]]] = {}
    for model, diff_sha in (("provider/a", "a" * 64), ("provider/b", "b" * 64)):
        artifacts[model] = [
            {
                "config": {"model": model},
                "benchmark": {
                    "case_id": "case-1",
                    "golden_url": "https://github.com/upstream/repo/pull/8",
                    "review_url": "https://github.com/fixture/repo/pull/1",
                },
                "pr": {
                    "base_sha": "c" * 40,
                    "head_sha": "d" * 40,
                },
                "diff": {**semantic_diff, "sha256": diff_sha},
            }
        ]

    with pytest.raises(cli.CliError, match="fixture snapshot differs"):
        cli._require_comparable_model_sweep(artifacts)

    for model in artifacts:
        artifacts[model][0]["diff"]["sha256"] = "e" * 64
    cli._require_comparable_model_sweep(artifacts)


def test_publish_with_yes_uses_separate_publisher_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "config": {"model": "codex/model"},
                "pr": {"url": "https://github.com/o/r/pull/1"},
                "findings": [],
            }
        ),
        encoding="utf-8",
    )

    @dataclass
    class Result:
        status: str = "published"
        finding_count: int = 0

    class Publisher:
        def __init__(self, client: Any) -> None:
            self.client = client

        def publish(self, _pr: Any, _artifact: Any, *, publish_clean: bool) -> Result:
            assert publish_clean is True
            return Result()

    monkeypatch.setattr(cli, "_github_types", lambda: (_Client, Publisher))
    args = cli.build_parser().parse_args(
        ["publish", str(artifact_path), "--yes", "--publish-clean"]
    )

    assert cli._publish(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "finding_count": 0,
        "status": "published",
    }


def test_main_redacts_direct_api_key_from_runtime_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk-direct-secret"

    async def fail(_argv: Any) -> int:
        raise RuntimeError(f"provider rejected {secret}")

    monkeypatch.setattr(cli, "async_main", fail)
    assert cli.main(["review-pr", "ignored", "--api-key", secret]) == 2
    error = capsys.readouterr().err
    assert secret not in error
    assert "[REDACTED]" in error

    api_base = "https://alice:password@gateway.example/v1/private-token"

    async def fail_with_endpoint(_argv: Any) -> int:
        raise RuntimeError(f"provider rejected endpoint {api_base}")

    monkeypatch.setattr(cli, "async_main", fail_with_endpoint)
    assert cli.main(["review-pr", "ignored", f"--api-base={api_base}"]) == 2
    endpoint_error = capsys.readouterr().err
    assert api_base not in endpoint_error
    assert "password" not in endpoint_error
    assert "private-token" not in endpoint_error


@pytest.mark.asyncio
async def test_benchmark_run_failure_path_records_redacted_error_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Engine.instances.clear()
    _Client.resolved.clear()
    secret = "sk-benchmark-failure-secret"
    monkeypatch.setenv("MARTIAN_API_KEY", secret)

    class _FailingEngine(_Engine):
        async def review(self, _pr: Any) -> _Artifact:
            raise RuntimeError(f"provider rejected token {secret}")

    case = SimpleNamespace(
        case_id="alpha",
        repository="upstream/alpha",
        golden_url="https://github.com/upstream/alpha/pull/10",
        review_url="https://github.com/fixture/alpha/pull/1",
        fixture_repo_name="fixture-alpha",
        fixture_tool="primarytool",
        golden_sha256="1" * 64,
    )
    manifest = SimpleNamespace(
        benchmark_sha256="b" * 64,
        golden_sha256="g" * 64,
        to_dict=lambda: {
            "case_count": 1,
            "benchmark_sha256": "b" * 64,
            "golden_sha256": "g" * 64,
        },
    )
    dataset = SimpleNamespace(cases=(case,), manifest=manifest)
    monkeypatch.setattr(
        cli,
        "_benchmark_api",
        lambda: (
            lambda *_a, **_k: dataset,
            object(),
            lambda model: model.replace("/", "_"),
            lambda model: model.replace("/", "_"),
        ),
    )
    monkeypatch.setattr(cli, "_engine_types", lambda: (_FailingEngine, _writer))
    monkeypatch.setattr(cli, "_repository_type", lambda: _Cache)
    monkeypatch.setattr(cli, "_github_types", lambda: (_Client, object))
    run_dir = tmp_path / "run"
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--benchmark-data",
            str(tmp_path / "benchmark_data.json"),
            "--model",
            "codex/a",
            "--run-dir",
            str(run_dir),
        ]
    )

    assert await cli._benchmark_run(args) == 1
    output = capsys.readouterr().out
    assert secret not in output
    run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["status_counts"] == {"failed": 1}
    record = run_manifest["records"][0]
    assert record["status"] == "failed"
    assert secret not in json.dumps(run_manifest)
    assert "[REDACTED]" in record["error"]


def test_argparse_error_channel_redacts_environment_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # argparse prints usage errors and exits before main()'s redaction
    # boundary exists; a credential mistyped into the wrong flag must not
    # print verbatim into captured CI logs.
    secret = "sk-super-secret-argparse-value"
    monkeypatch.setenv("MARTIAN_API_KEY", secret)
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(["review-pr", "https://example.com/pull/1", "--profile", secret])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert "[REDACTED]" in captured.err


def test_publish_exits_nonzero_when_a_clean_review_was_declined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        status = "clean_not_published"
        marker = "marker"
        findings = 0

        def to_dict(self) -> dict[str, Any]:
            return {"status": self.status, "marker": self.marker, "findings": self.findings}

    class Publisher:
        def __init__(self, client: Any) -> None:
            self.client = client

        def publish(self, pr: Any, artifact: Any, *, publish_clean: bool) -> Result:
            assert publish_clean is False
            return Result()

    class Client:
        def __init__(self, token: Any = None) -> None:
            self.token = token

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def resolve_pr(self, url: str) -> Any:
            return SimpleNamespace(url=url)

    monkeypatch.setattr(cli, "_github_types", lambda: (Client, Publisher))
    monkeypatch.setattr(cli, "_load_artifacts", lambda paths: [{"pr": {"url": "https://x/pull/1"}}])
    args = cli.build_parser().parse_args(["publish", str(tmp_path / "a.json"), "--yes"])
    assert cli._publish(args) == 1


def test_benchmark_judge_registers_resolved_credentials_for_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A dotenv-only key is in neither argv nor the environment; the judge
    # command must still register it at the main() redaction boundary.
    secret = "dotenv-only-judge-credential"
    env_file = tmp_path / ".env"
    env_file.write_text(f"MARTIAN_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.delenv("MARTIAN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_RUNTIME_SECRETS", set())

    async def failing_judge(**kwargs: Any) -> Any:
        raise RuntimeError(f"boom {secret}")

    import bugbunny.judge as judge_module

    monkeypatch.setattr(judge_module, "run_codereviewbench_judge", failing_judge)
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "judge",
            "--results-dir",
            str(tmp_path),
            "--judge-model",
            "openai/judge",
            "--env-file",
            str(env_file),
        ]
    )
    with pytest.raises(RuntimeError):
        asyncio.run(cli._benchmark_judge(args))
    assert secret in cli._RUNTIME_SECRETS


def test_verify_export_and_analyze_share_the_root_export_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unlocked verify or analyze racing a concurrent export could report
    # hashes for a bundle state that never coexisted with the checked bytes.
    locked_paths: list[Path] = []
    real_lock = cli.file_lock

    from contextlib import contextmanager

    @contextmanager
    def recording_lock(path: Path):
        locked_paths.append(path)
        with real_lock(path):
            yield

    monkeypatch.setattr(cli, "file_lock", recording_lock)

    results_dir = tmp_path / "results"
    judge_dir = results_dir / "openai_judge"
    judge_dir.mkdir(parents=True)
    manifest_path = judge_dir / "tool_export_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    verify_args = cli.build_parser().parse_args(
        ["benchmark", "verify-export", "--manifest", str(manifest_path)]
    )
    with pytest.raises(ValueError):
        cli._benchmark_verify_export(verify_args)
    assert results_dir.resolve() / ".bugbunny-export.lock" in locked_paths

    locked_paths.clear()
    analyze_args = cli.build_parser().parse_args(
        [
            "benchmark",
            "analyze",
            "--run-dir",
            str(tmp_path / "runs"),
            "--results-dir",
            str(results_dir),
            "--judge-model",
            "openai/judge",
        ]
    )
    with pytest.raises((cli.CliError, ValueError, FileNotFoundError)):
        cli._benchmark_analyze(analyze_args)
    assert results_dir.resolve() / ".bugbunny-export.lock" in locked_paths
