from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

import bugbunny.exploration as exploration_module
from bugbunny.exploration import (
    EXPLORATION_PROMPT_VERSION,
    EXPLORATION_SCHEMA_VERSION,
    ExplorationError,
    ExplorationResult,
    exploration_action_schema,
    exploration_prompt_sha256,
    exploration_schema_sha256,
    explore_repository_context,
)
from bugbunny.gateway import GatewayResult
from bugbunny.models import CallRecord
from bugbunny.repository import GrepHit, RepositoryLimitError


@dataclass(frozen=True)
class Config:
    context_mode: str = "agentic"
    context_selection_rounds: int = 2
    context_requests_per_round: int = 5
    max_context_files: int = 6
    max_context_chars: int = 4_000
    initial_context_chars: int = 500
    context_read_lines: int = 20
    context_read_chars: int = 800
    context_blob_read_bytes: int = 16_000_000
    context_search_hits: int = 6
    context_search_max_offset: int = 100_000
    repository_index_chars: int = 2_000
    timeout_seconds: int = 5
    reasoning_effort: str = "high"
    max_output_tokens: int = 512
    generation_input_char_budget: int | None = None


class FakeGateway:
    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = list(payloads)
        self.requests: list[dict[str, Any]] = []

    async def complete_json(self, prompt: str, **kwargs: Any) -> GatewayResult:
        self.requests.append({"prompt": prompt, **kwargs})
        payload = self.payloads.pop(0)
        return GatewayResult(
            payload=payload,
            call=CallRecord(
                stage=kwargs["stage"],
                gateway="fake",
                requested_model=kwargs["model"],
                resolved_model="fake-model",
                latency_ms=1,
                chunk_id=kwargs.get("chunk_id"),
                input_tokens=100,
                output_tokens=20,
            ),
        )


class FakeSnapshot:
    head_sha = "a" * 40

    def __init__(self) -> None:
        self.files = {
            "README.md": "project documentation\n",
            "src/core.py": "def target(value):\n    return helper(value)\n",
            "src/helper.py": "def helper(value):\n    return value + 1\n",
            "tests/test_core.py": "def test_target():\n    assert target(1) == 2\n",
        }
        self.read_calls: list[tuple[str, str, int]] = []
        self.grep_calls: list[dict[str, Any]] = []

    def read_blob(self, revision: str, path: str, *, max_bytes: int) -> str:
        self.read_calls.append((revision, path, max_bytes))
        value = self.files[path]
        if len(value.encode()) > max_bytes:
            raise ValueError("blob too large")
        return value

    def git_grep(self, pattern: str, **kwargs: Any) -> tuple[GrepHit, ...]:
        self.grep_calls.append({"pattern": pattern, **kwargs})
        scoped = kwargs.get("paths")
        hits: list[GrepHit] = []
        for path, value in sorted(self.files.items()):
            if scoped and not any(
                path == prefix or path.startswith(prefix + "/") for prefix in scoped
            ):
                continue
            for line, text in enumerate(value.splitlines(), 1):
                if pattern in text:
                    hits.append(GrepHit(path, line, text))
        return tuple(hits[: kwargs["limit"]])


def _action(
    action: str,
    *,
    path: str = "",
    query: str = "",
    start: int | None = None,
    end: int | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "path": path,
        "query": query,
        "start_line": start,
        "end_line": end,
    }


@pytest.mark.asyncio
async def test_two_round_loop_reads_only_immutable_head_and_returns_calls_and_metrics() -> None:
    gateway = FakeGateway(
        [
            {
                "requests": [
                    _action("list", path="tests"),
                    _action("search", query="target"),
                    _action("read", path="src/core.py", start=1, end=2),
                ],
                "done": False,
            },
            {
                "requests": [
                    _action("read", path="tests/test_core.py", start=1, end=2),
                ],
                "done": True,
            },
        ]
    )
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(),
        model="anthropic/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="+ return helper(value)",
        seed_context="seed definition",
        file_inventory=tuple(reversed(snapshot.files)),
        batch_id="batch-1",
    )

    assert isinstance(result, ExplorationResult)
    assert result.failed is False
    assert len(result.calls) == 2
    assert all(call.stage == "context_selection" for call in result.calls)
    assert all(call.chunk_id == "batch-1" for call in result.calls)
    assert all(request["max_output_tokens"] == 512 for request in gateway.requests)
    assert all(request["reasoning_effort"] == "high" for request in gateway.requests)
    assert "src/core.py" in result.context
    assert "tests/test_core.py" in result.context
    assert "return helper(value)" in result.context
    assert result.context.index("UNTRUSTED") < result.context.index("seed definition")
    assert len(result.context) <= Config.max_context_chars
    assert snapshot.read_calls
    assert all(revision == snapshot.head_sha for revision, _, _ in snapshot.read_calls)
    assert snapshot.grep_calls[0]["revision"] == snapshot.head_sha
    assert snapshot.grep_calls[0]["fixed"] is True
    assert snapshot.grep_calls[0]["paths"] is None

    trace = result.trace
    assert trace["rounds_completed"] == 2
    assert trace["selection_done"] is True
    assert trace["prompt_version"] == EXPLORATION_PROMPT_VERSION
    assert trace["schema_version"] == EXPLORATION_SCHEMA_VERSION
    assert trace["context_tokens_estimated"] == (len(result.context) + 3) // 4
    assert trace["context_files_exposed_to_model"] == sorted(["src/core.py", "tests/test_core.py"])
    assert trace["unique_context_files"] == 2
    assert trace["action_counts"] == {"list": 1, "read": 2, "search": 1}
    assert trace["round_limit_hit"] is False
    assert trace["request_limit_hit"] is False
    assert trace["file_limit_hit"] is False
    assert trace["context_limit_hit"] is False
    assert trace["failed"] is False


@pytest.mark.asyncio
async def test_invalid_actions_fail_closed_while_valid_actions_are_deduplicated() -> None:
    valid = _action("read", path="src/core.py", start=1, end=1)
    gateway = FakeGateway(
        [
            {
                "requests": [
                    _action("read", path="../secret", start=1, end=1),
                    _action("search", query="bad\nquery"),
                    _action("list", path="/unsafe"),
                    valid,
                    dict(valid),
                ],
                "done": True,
            }
        ]
    )
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert [(revision, path) for revision, path, _ in snapshot.read_calls] == [
        (snapshot.head_sha, "src/core.py")
    ]
    assert result.trace["requests_returned"] == 5
    assert result.trace["requests_rejected"] == 3
    assert result.trace["requests_deduplicated"] == 1
    assert result.trace["actions_executed"] == 1
    assert result.trace["request_cap_reached"] is True
    assert result.trace["request_limit_hit"] is False
    assert all(
        item["code"] not in {"invalid_payload", "selector_error"} for item in result.diagnostics
    )


@pytest.mark.asyncio
async def test_verbose_selector_is_capped_and_excess_requests_are_recorded() -> None:
    gateway = FakeGateway(
        [
            {
                "requests": [
                    _action("read", path="src/core.py", start=1, end=1),
                    _action("read", path="src/helper.py", start=1, end=1),
                    _action("read", path="tests/test_core.py", start=1, end=1),
                ],
                "done": True,
            }
        ]
    )
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(
            context_selection_rounds=1,
            context_requests_per_round=2,
            max_output_tokens=32_768,
        ),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert [path for _revision, path, _max_bytes in snapshot.read_calls] == [
        "src/core.py",
        "src/helper.py",
    ]
    assert gateway.requests[0]["max_output_tokens"] == 16_384
    assert result.trace["requests_returned"] == 3
    assert result.trace["requests_accepted"] == 2
    assert result.trace["requests_rejected"] == 1
    assert result.trace["requests_omitted_by_execution_cap"] == 1
    assert result.trace["request_cap_reached"] is True


@pytest.mark.asyncio
async def test_invalid_selector_payload_marks_result_failed_but_preserves_call() -> None:
    gateway = FakeGateway([{"requests": "not-an-array", "done": False}])
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="repository secret patch text",
        seed_context="safe seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is True
    assert result.context == "safe seed"
    assert len(result.calls) == 1
    assert result.trace["failed"] is True
    assert result.diagnostics == ({"stage": "context_selection", "code": "invalid_payload"},)
    serialized_diagnostics = json.dumps(result.diagnostics)
    assert "repository secret patch text" not in serialized_diagnostics


@pytest.mark.asyncio
async def test_curated_mode_is_a_bounded_zero_call_passthrough() -> None:
    gateway = FakeGateway([])
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_mode="curated", max_context_chars=40, initial_context_chars=20),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="x" * 100,
        file_inventory=tuple(snapshot.files),
    )

    assert len(result.context) == 20
    assert not result.calls
    assert not gateway.requests
    assert result.failed is False
    assert result.trace["mode"] == "curated"
    assert result.trace["context_tokens_estimated"] == 5
    assert result.trace["context_limit_hit"] is True


@pytest.mark.asyncio
async def test_file_and_character_budgets_are_hard_and_deterministic() -> None:
    payload = {
        "requests": [
            _action("search", query="def"),
            _action("read", path="README.md", start=1, end=1),
        ],
        "done": True,
    }
    config = Config(
        context_selection_rounds=1,
        max_context_files=1,
        max_context_chars=180,
        initial_context_chars=20,
        context_read_chars=160,
    )

    async def run_once() -> ExplorationResult:
        snapshot = FakeSnapshot()
        return await explore_repository_context(
            config=config,
            model="openai/test-model",
            gateway=FakeGateway([payload]),
            snapshot=snapshot,
            batch_patch="patch",
            seed_context="seed",
            file_inventory=tuple(reversed(snapshot.files)),
        )

    first = await run_once()
    second = await run_once()
    assert first.context == second.context
    assert first.trace == second.trace
    assert len(first.context) <= config.max_context_chars
    assert first.trace["unique_context_files"] == 1
    assert len(first.trace["context_files_exposed_to_model"]) == 1
    assert first.trace["requests_rejected"] == 1
    assert first.trace["file_limit_hit"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "failure", "expected_code"),
    [
        (_action("read", path="src/core.py", start=1, end=1), TimeoutError(), "action_timeout"),
        (_action("read", path="src/core.py", start=1, end=1), RuntimeError(), "read_failed"),
        (_action("search", query="target"), RuntimeError(), "search_failed"),
    ],
)
async def test_infrastructure_action_failures_fail_selection_coverage(
    action: dict[str, Any],
    failure: BaseException,
    expected_code: str,
) -> None:
    class FailingSnapshot(FakeSnapshot):
        def read_blob(self, revision: str, path: str, *, max_bytes: int) -> str:
            if action["action"] == "read":
                raise failure
            return super().read_blob(revision, path, max_bytes=max_bytes)

        def git_grep(self, pattern: str, **kwargs: Any) -> tuple[GrepHit, ...]:
            if action["action"] == "search":
                raise failure
            return super().git_grep(pattern, **kwargs)

    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway([{"requests": [action], "done": True}]),
        snapshot=FailingSnapshot(),
        batch_patch="patch",
        seed_context="safe seed",
        file_inventory=tuple(FakeSnapshot().files),
    )

    assert result.failed is True
    assert result.context == "safe seed"
    assert len(result.calls) == 1
    assert result.trace["round_limit_hit"] is False
    assert result.diagnostics == ({"stage": "context_action", "code": expected_code},)


@pytest.mark.asyncio
async def test_bounded_search_overflow_is_reported_without_failing_review() -> None:
    class LimitedSnapshot(FakeSnapshot):
        def git_grep(self, pattern: str, **kwargs: Any) -> tuple[GrepHit, ...]:
            raise RepositoryLimitError("bounded search output exceeded")

    gateway = FakeGateway(
        [
            {"requests": [_action("search", query="common_symbol")], "done": False},
            {"requests": [], "done": True},
        ]
    )
    result = await explore_repository_context(
        config=Config(context_selection_rounds=2),
        model="openai/test-model",
        gateway=gateway,
        snapshot=LimitedSnapshot(),
        batch_patch="patch",
        seed_context="safe seed",
        file_inventory=tuple(FakeSnapshot().files),
    )

    assert result.failed is False
    assert result.context.endswith("safe seed")
    assert "narrow the query or prefix" in result.context
    assert result.trace["search_scan_limit_hit"] is True
    assert result.trace["search_pagination_unresolved"] == 1
    assert "capped=Y" in gateway.requests[1]["prompt"]
    assert "narrow the query or prefix" in gateway.requests[1]["prompt"]


@pytest.mark.asyncio
async def test_inventory_list_does_not_consume_content_slots_and_limit_hits_are_logged() -> None:
    gateway = FakeGateway(
        [
            {"requests": [_action("list")], "done": False},
            {
                "requests": [_action("read", path="src/core.py", start=1, end=1)],
                "done": False,
            },
        ]
    )
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(
            context_selection_rounds=2,
            context_requests_per_round=1,
            max_context_files=1,
        ),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert "def target(value):" in result.context
    assert result.trace["context_files_exposed_to_model"] == ["src/core.py"]
    assert result.trace["unique_context_files"] == 1
    assert result.trace["request_limit_hit"] is True
    assert result.trace["round_limit_hit"] is True
    assert result.trace["file_limit_hit"] is False


@pytest.mark.asyncio
async def test_inventory_list_cursor_pages_past_alphabetical_prefix() -> None:
    gateway = FakeGateway(
        [
            {"requests": [_action("list")], "done": False},
            {
                "requests": [_action("list", query="src/core.py")],
                "done": True,
            },
        ]
    )
    snapshot = FakeSnapshot()

    result = await explore_repository_context(
        config=Config(context_search_hits=2),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert result.context == "seed"
    assert result.trace["list_hits"] == 4
    assert result.trace["selector_observations_truncated"] is False
    assert result.trace["list_page_limit_hit"] is True
    assert result.trace["list_pagination_unresolved"] == 0
    assert "README.md" in gateway.requests[1]["prompt"]
    assert "src/core.py" in gateway.requests[1]["prompt"]


@pytest.mark.asyncio
async def test_search_hit_cap_is_visible_in_trace() -> None:
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1, context_search_hits=1),
        model="openai/test-model",
        gateway=FakeGateway([{"requests": [_action("search", query="target")], "done": True}]),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert result.trace["search_hits"] == 1
    assert result.trace["search_hit_limit_hit"] is True


@pytest.mark.asyncio
async def test_search_pages_can_reach_results_after_the_first_cap() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {"requests": [_action("search", query="target")], "done": False},
            {"requests": [_action("search", query="target", start=2)], "done": False},
            {"requests": [_action("search", query="target", start=3)], "done": True},
        ]
    )
    result = await explore_repository_context(
        config=Config(context_selection_rounds=3, context_search_hits=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert "src/core.py:1" in result.context
    assert "tests/test_core.py:1" in result.context
    assert "next_start=00000002" in gateway.requests[1]["prompt"]
    assert "next_start=00000003" in gateway.requests[2]["prompt"]
    assert snapshot.grep_calls[0]["limit"] == 2
    assert snapshot.grep_calls[1]["limit"] == 3
    assert snapshot.grep_calls[2]["limit"] == 4
    assert result.trace["search_hit_limit_hit"] is True
    assert result.trace["search_pagination_unresolved"] == 0
    assert result.trace["context_truncated"] is False
    assert result.trace["context_limit_hit"] is False


@pytest.mark.asyncio
async def test_long_search_row_exposes_its_location_before_clipping_text() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["a.py"] = "needle " + "x" * 1_000 + "\n"
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1, context_search_hits=1, context_read_chars=220),
        model="openai/test-model",
        gateway=FakeGateway([{"requests": [_action("search", query="needle")], "done": True}]),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert "a.py:1:" in result.context
    assert result.trace["search_hits"] == 1
    assert result.trace["context_truncated"] is True
    assert result.trace["context_limit_hit"] is True
    assert result.trace["search_hit_limit_hit"] is False


@pytest.mark.asyncio
async def test_file_cap_still_allows_later_ranges_from_an_already_selected_file() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {
                "requests": [_action("read", path="src/core.py", start=1, end=1)],
                "done": False,
            },
            {
                "requests": [_action("read", path="src/core.py", start=2, end=2)],
                "done": True,
            },
        ]
    )
    result = await explore_repository_context(
        config=Config(max_context_files=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert "def target(value):" in result.context
    assert "return helper(value)" in result.context
    assert result.trace["rounds_completed"] == 2
    assert result.trace["additional_context_files_selected"] == 1
    assert result.trace["file_limit_hit"] is False


@pytest.mark.asyncio
async def test_valid_leading_dash_path_can_be_selected() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["-root.py"] = "value = 1\n"
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [_action("read", path="-root.py", start=1, end=1)],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert "value = 1" in result.context
    assert result.trace["context_files_exposed_to_model"] == ["-root.py"]


@pytest.mark.asyncio
async def test_action_inaccessible_inventory_path_is_omitted_with_telemetry() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["x" * 4_097] = "value = 1\n"
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway([{"requests": [], "done": True}]),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert result.trace["repository_files"] == len(snapshot.files) - 1
    assert result.trace["repository_files_total"] == len(snapshot.files)
    assert result.trace["repository_files_omitted_from_selector_inventory"] == 1
    assert result.trace["repository_inventory_omission_hit"] is True


@pytest.mark.asyncio
async def test_whole_tree_search_overfetches_past_omitted_inventory_hits() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["0" * 4_097] = "rare_target\n"
    snapshot.files["a.py"] = "rare_target\n"
    snapshot.files["b.py"] = "rare_target\n"
    gateway = FakeGateway(
        [
            {"requests": [_action("search", query="rare_target")], "done": False},
            {
                "requests": [_action("search", query="rare_target", start=2)],
                "done": True,
            },
        ]
    )
    result = await explore_repository_context(
        config=Config(context_search_hits=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert "a.py:1:" in result.context
    assert "b.py:1:" in result.context
    assert result.trace["search_pagination_unresolved"] == 0
    assert result.trace["search_scan_limit_hit"] is False
    assert [call["limit"] for call in snapshot.grep_calls] == [2, 4, 3, 6]


@pytest.mark.asyncio
async def test_search_offset_cap_is_schema_enforced_and_auditable() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {"requests": [_action("search", query="target")], "done": False},
            {"requests": [_action("search", query="target", start=2)], "done": False},
            {"requests": [_action("search", query="target", start=3)], "done": True},
        ]
    )
    result = await explore_repository_context(
        config=Config(
            context_selection_rounds=3,
            context_search_hits=1,
            context_search_max_offset=2,
        ),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    start_schema = gateway.requests[0]["schema"]["properties"]["requests"]["items"]["properties"][
        "start_line"
    ]
    assert "maximum" not in start_schema
    assert "capped=Y" in gateway.requests[2]["prompt"]
    assert result.trace["requests_rejected"] == 1
    assert result.trace["search_offset_limit_hit"] is True
    assert result.trace["search_pagination_unresolved"] == 1


@pytest.mark.asyncio
async def test_search_offset_cap_does_not_restrict_read_line_numbers() -> None:
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1, context_search_max_offset=2),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [_action("read", path="src/core.py", start=3, end=3)],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.trace["requests_rejected"] == 0
    assert result.trace["actions_executed"] == 1


@pytest.mark.asyncio
async def test_search_and_list_cursors_must_come_from_a_prior_selector_round() -> None:
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [
                        _action("search", query="target", start=100_000),
                        _action("list", query="src/core.py"),
                    ],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert snapshot.grep_calls == []
    assert result.trace["requests_accepted"] == 0
    assert result.trace["requests_rejected"] == 2
    assert result.trace["cursor_requests_rejected"] == 2
    assert result.diagnostics == (
        {"stage": "context_action", "code": "invalid_search_cursor"},
        {"stage": "context_action", "code": "invalid_list_cursor"},
    )


@pytest.mark.asyncio
async def test_only_exact_cursor_from_previous_round_is_accepted() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {
                "requests": [
                    _action("search", query="target"),
                    _action("list"),
                ],
                "done": False,
            },
            {
                "requests": [
                    _action("search", query="target", start=3),
                    _action("list", query="src/core.py"),
                ],
                "done": True,
            },
        ]
    )
    result = await explore_repository_context(
        config=Config(context_search_hits=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.trace["actions_executed"] == 2
    assert result.trace["cursor_requests_rejected"] == 2
    assert len(snapshot.grep_calls) == 1


@pytest.mark.asyncio
async def test_predicted_cursors_rejected_in_one_round_can_be_used_after_they_are_offered() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {
                "requests": [
                    _action("search", query="target"),
                    _action("search", query="target", start=2),
                    _action("list"),
                    _action("list", query="README.md"),
                ],
                "done": False,
            },
            {
                "requests": [
                    _action("search", query="target", start=2),
                    _action("list", query="README.md"),
                ],
                "done": True,
            },
        ]
    )
    result = await explore_repository_context(
        config=Config(context_requests_per_round=4, context_search_hits=1),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.trace["cursor_requests_rejected"] == 2
    assert result.trace["actions_executed"] == 4
    assert result.trace["search_hits"] == 2
    assert result.trace["list_hits"] == 2
    assert len(snapshot.grep_calls) == 2


@pytest.mark.asyncio
async def test_null_and_one_search_starts_are_deduplicated_as_the_same_first_page() -> None:
    snapshot = FakeSnapshot()
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [
                        _action("search", query="target"),
                        _action("search", query="target", start=1),
                    ],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.trace["actions_executed"] == 1
    assert result.trace["requests_deduplicated"] == 1
    assert len(snapshot.grep_calls) == 1


@pytest.mark.asyncio
async def test_too_small_repository_index_bound_fails_before_selection() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway([])
    with pytest.raises(ExplorationError, match="disclose truncation"):
        await explore_repository_context(
            config=Config(repository_index_chars=10),
            model="openai/test-model",
            gateway=gateway,
            snapshot=snapshot,
            batch_patch="patch",
            seed_context="seed",
            file_inventory=tuple(snapshot.files),
        )

    assert gateway.requests == []


@pytest.mark.asyncio
async def test_render_limited_list_page_exposes_more_marker() -> None:
    snapshot = FakeSnapshot()
    gateway = FakeGateway(
        [
            {"requests": [_action("list")], "done": False},
            {"requests": [], "done": True},
        ]
    )
    result = await explore_repository_context(
        config=Config(context_read_chars=70),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.trace["list_page_limit_hit"] is False
    assert result.trace["list_pagination_unresolved"] == 1
    assert result.trace["list_hits"] == 0
    assert "INVENTORY LIST more=Y" in gateway.requests[1]["prompt"]


@pytest.mark.asyncio
async def test_small_line_read_from_large_blob_uses_configured_blob_bound() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["large.py"] = "first line\n" + "x" * 1_100_000
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [_action("read", path="large.py", start=1, end=1)],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert "first line" in result.context
    assert snapshot.read_calls[-1][2] == Config.context_blob_read_bytes
    assert result.trace["blob_read_limit_hit"] is False


@pytest.mark.asyncio
async def test_defensive_repository_index_fit_retains_truncation_marker() -> None:
    snapshot = FakeSnapshot()
    for number in range(200):
        snapshot.files[f"very/long/repository/path/{number:04d}-module.py"] = "value = 1\n"
    gateway = FakeGateway([{"requests": [], "done": True}])
    result = await explore_repository_context(
        config=Config(
            context_selection_rounds=1,
            max_context_chars=8_000,
            repository_index_chars=8_000,
            generation_input_char_budget=5_000,
        ),
        model="openai/test-model",
        gateway=gateway,
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert result.failed is False
    assert result.trace["repository_index_truncated"] is True
    assert "repository index truncated" in gateway.requests[0]["prompt"]


@pytest.mark.asyncio
async def test_scoped_search_requests_literal_git_paths() -> None:
    snapshot = FakeSnapshot()
    snapshot.files["src/*.py"] = "literal_target()\n"
    snapshot.files["src/other.py"] = "literal_target()\n"
    result = await explore_repository_context(
        config=Config(context_selection_rounds=1),
        model="openai/test-model",
        gateway=FakeGateway(
            [
                {
                    "requests": [_action("search", path="src/*.py", query="literal_target")],
                    "done": True,
                }
            ]
        ),
        snapshot=snapshot,
        batch_patch="patch",
        seed_context="seed",
        file_inventory=tuple(snapshot.files),
    )

    assert "src/*.py:1" in result.context
    assert "src/other.py:1" not in result.context
    assert snapshot.grep_calls[-1]["literal_paths"] is True


def test_prompt_and_schema_contracts_are_versioned_stable_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = exploration_action_schema(3)
    assert schema["properties"]["requests"]["maxItems"] == 64
    action_schema = schema["properties"]["requests"]["items"]
    assert action_schema["additionalProperties"] is False
    assert set(action_schema["required"]) == {
        "action",
        "path",
        "query",
        "start_line",
        "end_line",
    }
    prompt_hash = exploration_prompt_sha256()
    assert EXPLORATION_PROMPT_VERSION == "bugbunny-context-selection-v6"
    assert EXPLORATION_SCHEMA_VERSION == "bugbunny-context-actions-v5"
    assert len(prompt_hash) == 64
    monkeypatch.setattr(
        exploration_module,
        "EXPLORATION_SYSTEM_PROMPT",
        exploration_module.EXPLORATION_SYSTEM_PROMPT + "\nAdditional trusted instruction.",
    )
    assert exploration_prompt_sha256() != prompt_hash
    assert len(exploration_schema_sha256(3)) == 64
    assert exploration_schema_sha256(3) != exploration_schema_sha256(4)
    assert exploration_schema_sha256(3, 10) != exploration_schema_sha256(3, 11)


def test_repository_index_does_not_truncate_when_the_complete_index_fits() -> None:
    assert exploration_module._render_index(("README.md",), 64) == ("README.md", False)
