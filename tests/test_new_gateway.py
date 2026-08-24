from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from bugbunny.gateway import (
    GatewayConfig,
    GatewayError,
    ModelGateway,
    ResponseFormatError,
    extract_json_object,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {"type": "object"}}},
}


def _response(content: str) -> dict[str, object]:
    return {
        "model": "resolved-model",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 31,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 11},
            "cost": 0.00125,
        },
    }


@pytest.mark.asyncio
async def test_gateway_enforces_one_limit_across_concurrent_reviews() -> None:
    active = 0
    maximum = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=_response('{"findings":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            GatewayConfig(api_key="test-key", max_retries=0),
            http_client=client,
            max_concurrency=3,
        )
        results = await asyncio.gather(
            *(
                gateway.complete_json(
                    f"review {index}",
                    model="openai/gpt-test",
                    stage="generation",
                    schema_name="findings",
                    schema=SCHEMA,
                )
                for index in range(12)
            )
        )

    assert maximum == 3
    assert len(results) == 12


@pytest.mark.asyncio
async def test_martian_call_is_structured_provider_prefixed_and_secret_free():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response('{"findings":[]}'))

    secret = "sk-test-super-secret-value"
    gateway = ModelGateway(GatewayConfig(api_key=secret, api_base="https://gateway.example/v1"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(gateway.config, http_client=client)
        result = await gateway.complete_json(
            "review this patch",
            model="openai/gpt-test",
            stage="generation",
            chunk_id="chunk-7",
            reasoning_effort="medium",
            schema_name="bugbunny_findings",
            schema=SCHEMA,
        )

    body = captured["body"]
    assert isinstance(body, dict)
    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["authorization"] == f"Bearer {secret}"
    assert body["model"] == "openai/gpt-test"
    assert body["reasoning_effort"] == "medium"
    assert "temperature" not in body
    assert body["max_completion_tokens"] == 32768
    assert body["response_format"] == {"type": "json_object"}
    assert body["martian_metadata"] == {
        "application": "bugbunny",
        "schema_name": "bugbunny_findings",
    }
    assert result.payload == {"findings": []}
    assert result.call.gateway == "martian_http"
    assert result.call.requested_model == "openai/gpt-test"
    assert result.call.resolved_model == "resolved-model"
    assert result.call.chunk_id == "chunk-7"
    assert result.call.input_tokens == 31
    assert result.call.output_tokens == 7
    assert result.call.cached_input_tokens == 11
    assert result.call.cost_usd == 0.00125
    assert len(result.call.response_sha256 or "") == 64
    assert len(result.call.request_sha256 or "") == 64
    assert len(result.call.schema_sha256 or "") == 64
    assert secret not in repr(gateway.config)
    assert secret not in json.dumps(result.call.to_dict())


@pytest.mark.asyncio
async def test_call_specific_output_limit_overrides_shared_gateway_default() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_response('{"findings":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ModelGateway(
            GatewayConfig(api_key="test-key", max_output_tokens=32_768),
            http_client=client,
        ).complete_json(
            "select context",
            model="openai/gpt-test",
            stage="context_selection",
            schema_name="selection",
            schema=SCHEMA,
            max_output_tokens=4_096,
        )

    assert captured["max_completion_tokens"] == 4_096


@pytest.mark.asyncio
async def test_martian_falls_back_only_when_json_mode_is_unsupported():
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format is unsupported"}},
            )
        return httpx.Response(200, json=_response('```json\n{"findings": []}\n```'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelGateway(
            GatewayConfig(api_key="test-key", max_retries=0), http_client=client
        ).complete_json(
            "review",
            model="anthropic/claude-test",
            stage="generation",
            reasoning_effort="high",
            schema_name="findings",
            schema=SCHEMA,
        )

    assert result.payload == {"findings": []}
    assert "response_format" in calls[0]
    assert "reasoning_effort" not in calls[0]
    assert "response_format" not in calls[1]
    assert "reasoning_effort" not in calls[1]
    assert calls[1]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_martian_retries_transient_statuses():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(200, json=_response('{"findings":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelGateway(
            GatewayConfig(api_key="test-key", max_retries=1), http_client=client
        ).complete_json(
            "review",
            model="openai/gpt-test",
            stage="generation",
            schema_name="findings",
            schema=SCHEMA,
        )

    assert attempts == 2
    assert result.payload == {"findings": []}
    assert result.call.attempt_count == 2
    assert result.call.retry_errors == ("HTTP 429",)


@pytest.mark.asyncio
async def test_martian_retries_locally_schema_invalid_success_response():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = '{"wrong":[]}' if attempts == 1 else '{"findings":[]}'
        return httpx.Response(200, json=_response(content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelGateway(
            GatewayConfig(api_key="test-key", max_retries=1), http_client=client
        ).complete_json(
            "review",
            model="openai/gpt-test",
            stage="generation",
            schema_name="findings",
            schema=SCHEMA,
        )

    assert attempts == 2
    assert result.payload == {"findings": []}
    assert result.call.attempt_count == 2
    assert result.call.input_tokens == 62
    assert result.call.output_tokens == 14
    assert result.call.cached_input_tokens == 22
    assert result.call.cost_usd == 0.0025
    assert result.call.retry_errors == ("ResponseFormatError: $.findings is required",)


@pytest.mark.asyncio
async def test_martian_retries_unparseable_json_success_response():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = "not json" if attempts == 1 else '{"findings":[]}'
        return httpx.Response(200, json=_response(content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ModelGateway(
            GatewayConfig(api_key="test-key", max_retries=1), http_client=client
        ).complete_json(
            "review",
            model="openai/gpt-test",
            stage="generation",
            schema_name="findings",
            schema=SCHEMA,
        )

    assert attempts == 2
    assert result.call.attempt_count == 2
    assert result.call.input_tokens == 62
    assert result.call.output_tokens == 14
    assert result.call.cached_input_tokens == 22
    assert len(result.call.retry_errors) == 1
    assert "could not parse model JSON" in result.call.retry_errors[0]


@pytest.mark.asyncio
async def test_gateway_error_carries_redacted_call_record():
    secret = "sk-test-secret-12345678"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"invalid api_key={secret}"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(GatewayConfig(api_key=secret, max_retries=0), http_client=client)
        with pytest.raises(GatewayError) as caught:
            await gateway.complete_json(
                "review",
                model="openai/gpt-test",
                stage="generation",
                schema_name="findings",
                schema=SCHEMA,
            )

    assert caught.value.call.error
    assert "[REDACTED]" in caught.value.call.error
    assert secret not in str(caught.value)
    assert secret not in json.dumps(caught.value.call.to_dict())


@pytest.mark.asyncio
async def test_gateway_redacts_credential_bearing_api_base_from_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    api_base = "https://alice:ultra-secret-pass@gateway.example/v1/private-token"
    # The component secret is discovered separately; the longer containing URL
    # still has to be redacted first so its opaque path cannot survive.
    monkeypatch.setenv("CUSTOM_API_KEY", "ultra-secret-pass")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"provider connection failed at {api_base}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            GatewayConfig(api_key_env="CUSTOM_API_KEY", api_base=api_base, max_retries=0),
            http_client=client,
        )
        with pytest.raises(GatewayError) as caught:
            await gateway.complete_json(
                "review",
                model="openai/gpt-test",
                stage="generation",
                schema_name="findings",
                schema=SCHEMA,
            )

    serialized = json.dumps(caught.value.call.to_dict())
    assert "[REDACTED]" in serialized
    for sensitive in (api_base, "alice", "ultra-secret-pass", "private-token"):
        assert sensitive not in str(caught.value)
        assert sensitive not in serialized
        assert sensitive not in repr(gateway.config)


@pytest.mark.asyncio
async def test_schema_failure_preserves_backend_response_telemetry():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response('{"findings":"not-an-array"}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as caught:
            await ModelGateway(
                GatewayConfig(api_key="test-key", max_retries=0), http_client=client
            ).complete_json(
                "review",
                model="openai/gpt-test",
                stage="generation",
                chunk_id="chunk-invalid",
                schema_name="findings",
                schema=SCHEMA,
            )

    call = caught.value.call
    assert call.resolved_model == "resolved-model"
    assert call.input_tokens == 31
    assert call.output_tokens == 7
    assert call.cached_input_tokens == 11
    assert call.cost_usd == 0.00125
    assert len(call.response_sha256 or "") == 64
    assert call.chunk_id == "chunk-invalid"
    assert "required type" in (call.error or "")


@pytest.mark.asyncio
async def test_empty_reasoning_response_reports_truncation_telemetry():
    response = _response("")
    response["choices"] = [{"finish_reason": "length", "message": {"content": ""}}]
    usage = response["usage"]
    assert isinstance(usage, dict)
    usage["completion_tokens"] = 8192
    usage["completion_tokens_details"] = {"reasoning_tokens": 8192}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GatewayError) as caught:
            await ModelGateway(
                GatewayConfig(api_key="test-key", max_retries=0), http_client=client
            ).complete_json(
                "review",
                model="openai/gpt-test",
                stage="generation",
                schema_name="findings",
                schema=SCHEMA,
            )

    error = caught.value.call.error or ""
    assert "finish_reason='length'" in error
    assert "completion_tokens=8192" in error
    assert "reasoning_tokens=8192" in error


@pytest.mark.asyncio
async def test_gateway_redacts_credentials_discovered_implicitly_from_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "sk-implicit-martian-secret-should-never-persist"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"rejected {secret}"}})

    monkeypatch.setenv("MARTIAN_API_KEY", secret)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(GatewayConfig(max_retries=0), http_client=client)
        with pytest.raises(GatewayError) as caught:
            await gateway.complete_json(
                "review",
                model="google/test",
                stage="generation",
                schema_name="findings",
                schema=SCHEMA,
            )

    serialized = json.dumps(caught.value.call.to_dict())
    assert "[REDACTED]" in serialized
    assert secret not in serialized
    assert secret not in str(caught.value)


def test_gateway_reads_martian_key_from_explicit_dotenv_without_serializing_it(tmp_path: Path):
    secret = "sk-local-dotenv-secret"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"MARTIAN_API_KEY={secret}\n", encoding="utf-8")
    config = GatewayConfig(dotenv_path=dotenv)

    assert config.resolved_api_key() == secret
    provenance = config.runtime_provenance("openai/gpt-test")
    assert provenance["auth_mode"] == "martian_dotenv"
    assert provenance["credential_configured"] is True
    assert secret not in repr(config)
    assert secret not in json.dumps(provenance)


@pytest.mark.asyncio
async def test_codex_uses_isolated_noninteractive_current_login_command(
    monkeypatch: pytest.MonkeyPatch,
):
    capture = {}
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("CODEX_HOME", "/safe/codex-home")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-reach-codex")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-must-not-reach-codex")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-must-not-reach-codex")
    monkeypatch.setenv("ARBITRARY_CALLER_VALUE", "must-not-be-inherited")

    class FakeProcess:
        returncode = 0

        async def communicate(self, input):
            capture["stdin"] = input.decode("utf-8")
            args = capture["args"]
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text('{"findings":[]}', encoding="utf-8")
            event = {
                "type": "turn.completed",
                "model": "gpt-test-resolved",
                "usage": {
                    "input_tokens": 41,
                    "cached_input_tokens": 13,
                    "output_tokens": 5,
                },
            }
            return (json.dumps(event).encode("utf-8") + b"\n", b"")

        def kill(self):
            raise AssertionError("successful process must not be killed")

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        capture["args"] = list(args)
        capture["kwargs"] = kwargs
        capture["cwd_was_empty"] = not any(Path(kwargs["cwd"]).iterdir())
        schema_path = Path(args[args.index("--output-schema") + 1])
        capture["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        return FakeProcess()

    gateway = ModelGateway(GatewayConfig(codex_executable="/opt/bin/codex", max_retries=0))
    with patch("bugbunny.gateway.asyncio.create_subprocess_exec", new=spawn):
        result = await gateway.complete_json(
            "untrusted patch",
            model="codex/gpt-test",
            stage="verification",
            chunk_id="batch-1",
            reasoning_effort="high",
            schema_name="findings",
            schema=SCHEMA,
            system_prompt="trusted system",
        )

    args = capture["args"]
    assert args[:2] == ["/opt/bin/codex", "exec"]
    for required in (
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        "--json",
        "--output-last-message",
    ):
        assert required in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "gpt-test"
    assert args[args.index("--config") + 1] == 'model_reasoning_effort="high"'
    disabled = [args[index + 1] for index, value in enumerate(args) if value == "--disable"]
    for feature in (
        "apps",
        "browser_use",
        "code_mode_host",
        "image_generation",
        "multi_agent",
        "shell_tool",
        "skill_search",
        "tool_suggest",
        "unified_exec",
    ):
        assert feature in disabled
    assert args[-1] == "-"
    assert capture["cwd_was_empty"] is True
    child_env = capture["kwargs"]["env"]
    assert child_env["HOME"] == "/safe/home"
    assert child_env["CODEX_HOME"] == "/safe/codex-home"
    for forbidden in (
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "ARBITRARY_CALLER_VALUE",
    ):
        assert forbidden not in child_env
    assert capture["schema"] == SCHEMA
    assert capture["stdin"] == "trusted system\n\nuntrusted patch"
    assert result.call.gateway == "codex_cli"
    assert result.call.resolved_model == "gpt-test-resolved"
    assert result.call.input_tokens == 41
    assert result.call.cached_input_tokens == 13
    assert result.call.output_tokens == 5
    assert result.call.cost_usd is None


def test_runtime_provenance_is_reproducible_and_never_contains_secrets(monkeypatch):
    secret = "sk-provenance-must-never-appear"
    monkeypatch.setenv("CUSTOM_PROVIDER_API_KEY", secret)
    config = GatewayConfig(
        api_key=secret,
        api_key_env="CUSTOM_PROVIDER_API_KEY",
        api_base="https://user:password@gateway.example/private?api_key=top-secret",
        timeout_seconds=17,
        max_retries=3,
        max_output_tokens=456,
        temperature=0.25,
    )
    with patch("bugbunny.gateway.importlib.metadata.version", return_value="1.2.3"):
        provenance = ModelGateway(config).runtime_provenance("openai/gpt-test")

    serialized = json.dumps(provenance, sort_keys=True)
    assert provenance["requested_model"] == "openai/gpt-test"
    assert provenance["transport"] == "martian_http"
    assert provenance["transport_version"] == "1.2.3"
    assert provenance["auth_mode"] == "explicit_api_key"
    assert provenance["credential_configured"] is True
    assert provenance["limits"] == {
        "timeout_seconds": 17,
        "max_retries": 3,
        "max_output_tokens": 456,
        "max_output_tokens_transport_applied": True,
        "temperature": 0.25,
        "temperature_applied": False,
        "reasoning_effort_parameter_will_be_sent": True,
    }
    assert provenance["api_base"]["host"] == "gateway.example"
    assert len(provenance["api_base"]["sha256"]) == 64
    for sensitive in (secret, "user", "password", "/private", "top-secret"):
        assert sensitive not in serialized


def test_codex_runtime_provenance_reports_current_login_and_bounded_version():
    completed = SimpleNamespace(
        stdout="codex-cli 0.148.0-alpha.15\naccidental-secret-output",
        stderr="",
    )
    config = GatewayConfig(
        api_key="ignored-secret",
        api_base="https://ignored.example/private",
        codex_executable="/opt/bin/codex",
    )
    with patch("bugbunny.gateway.subprocess.run", return_value=completed) as run:
        provenance = config.runtime_provenance("codex/gpt-test")

    run.assert_called_once_with(
        ["/opt/bin/codex", "--version"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    assert provenance["auth_mode"] == "codex_current_login"
    assert provenance["transport"] == "codex_cli"
    assert provenance["transport_version"] == "0.148.0-alpha.15"
    assert provenance["credential_configured"] is False
    assert provenance["limits"]["max_output_tokens_transport_applied"] is False
    assert provenance["limits"]["reasoning_effort_parameter_will_be_sent"] is True
    assert provenance["api_base"] == {
        "configured": False,
        "host": None,
        "sha256": None,
    }
    assert "accidental-secret-output" not in json.dumps(provenance)


def test_runtime_provenance_marks_non_openai_martian_reasoning_parameter_omitted():
    provenance = GatewayConfig(api_key="test-key").runtime_provenance("anthropic/claude-opus-4-5")

    assert provenance["limits"]["reasoning_effort_parameter_will_be_sent"] is False
    assert provenance["limits"]["temperature_applied"] is True


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
    ],
)
@pytest.mark.asyncio
async def test_martian_omits_temperature_for_routes_that_reject_it(model: str):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_response('{"findings":[]}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            GatewayConfig(api_key="test-key", temperature=0.25),
            http_client=client,
        )
        result = await gateway.complete_json(
            "review",
            model=model,
            stage="generation",
            schema_name="findings",
            schema=SCHEMA,
        )

    assert result.payload == {"findings": []}
    assert "temperature" not in captured
    provenance = gateway.runtime_provenance(model)
    assert provenance["limits"]["temperature"] == 0.25
    assert provenance["limits"]["temperature_applied"] is False


def test_json_extraction_handles_fences_prose_and_rejects_duplicates():
    assert extract_json_object('answer:\n```json\n{"ok": true}\n```') == {"ok": True}
    assert extract_json_object('preface {"ok": true} trailing') == {"ok": True}
    with pytest.raises(ResponseFormatError):
        extract_json_object('{"ok": true, "ok": false}')


@pytest.mark.asyncio
async def test_model_names_must_be_provider_prefixed():
    with pytest.raises(ValueError, match="provider-prefixed"):
        await ModelGateway().complete_json(
            "review",
            model="gpt-test",
            stage="generation",
            schema_name="findings",
            schema=SCHEMA,
        )
