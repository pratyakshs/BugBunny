from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from bugbunny import __version__
from bugbunny.models import CallRecord
from bugbunny.util import acquire_semaphore_bounded, is_finite_number

MARTIAN_API_BASE = "https://api.withmartian.com/v1"
MARTIAN_API_KEY_ENV = "MARTIAN_API_KEY"

# These catalog routes reject the otherwise-supported ``temperature`` field
# with HTTP 400. Keep the compatibility rule explicit and model-scoped so the
# fixed-temperature behavior of existing routes (including the benchmark
# verifier and judge) does not change.
_MARTIAN_MODELS_WITHOUT_TEMPERATURE = frozenset(
    {
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
    }
)
_MARTIAN_NON_OPENAI_MODELS_WITH_REASONING_EFFORT = frozenset(
    {
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
    }
)

# ``codex exec`` is an autonomous agent binary, not a plain completion HTTP
# client.  Its child environment and tool surface therefore need a much tighter
# boundary than the in-process Martian HTTP adapter: review prompts contain
# attacker-controlled repository text, while the parent process may hold cloud
# and GitHub credentials for unrelated providers.
_CODEX_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_CODEX_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "enable_mcp_apps",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "plugins",
    "plugin_sharing",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "workspace_dependencies",
)


def _codex_environment() -> dict[str, str]:
    """Return only non-credential runtime paths/locales needed by Codex.

    In particular, API keys, GitHub tokens, proxy credentials, shell startup
    variables, and arbitrary caller variables are not inherited. ``HOME`` and
    ``CODEX_HOME`` remain available so the binary can use the already logged-in
    Codex session; model-visible tools are disabled separately below.
    """

    return {
        name: value for name in _CODEX_ENV_ALLOWLIST if (value := os.environ.get(name)) is not None
    }


def _martian_uses_temperature(model: str) -> bool:
    provider, _ = _model_parts(model)
    return provider != "openai" and model not in _MARTIAN_MODELS_WITHOUT_TEMPERATURE


def _martian_uses_reasoning_effort(model: str) -> bool:
    provider, _ = _model_parts(model)
    return provider == "openai" or model in _MARTIAN_NON_OPENAI_MODELS_WITH_REASONING_EFFORT


class GatewayError(RuntimeError):
    """A bounded model call failed and carries its secret-free telemetry."""

    def __init__(self, message: str, call: CallRecord):
        super().__init__(message)
        self.call = call


class ResponseFormatError(ValueError):
    """The model response is not valid JSON for the requested schema."""


class _BackendFailure(RuntimeError):
    """Internal retry-aware failure boundary; rendered safely by the caller."""

    def __init__(
        self,
        error: BaseException,
        attempt_count: int,
        retry_errors: Sequence[str],
        *,
        backend: _BackendResult | None = None,
        response_sha256: str | None = None,
    ):
        super().__init__(str(error))
        self.error = error
        self.attempt_count = attempt_count
        self.retry_errors = tuple(retry_errors)
        self.backend = backend
        self.response_sha256 = response_sha256


def _dotenv_value(path: Path, name: str) -> str | None:
    """Read one value from a dotenv file without executing shell syntax."""

    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot read model credential file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            if value.startswith('"'):
                # Scan past backslash escapes so json.loads parses exactly the
                # quoted span; a trailing comment must not join the value.
                end = None
                index = 1
                while index < len(value):
                    if value[index] == "\\":
                        index += 2
                        continue
                    if value[index] == '"':
                        end = index
                        break
                    index += 1
                if end is None:
                    raise RuntimeError(f"invalid quoted {name} in {path} at line {line_number}")
                try:
                    parsed = json.loads(value[: end + 1])
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"invalid quoted {name} in {path} at line {line_number}"
                    ) from exc
                if not isinstance(parsed, str):
                    raise RuntimeError(f"invalid {name} in {path} at line {line_number}")
                unquoted = parsed
            else:
                end = value.find("'", 1)
                if end < 0:
                    raise RuntimeError(f"invalid quoted {name} in {path} at line {line_number}")
                unquoted = value[1:end]
            remainder = value[end + 1 :]
            if remainder.strip() and not re.fullmatch(r"\s+#.*", remainder):
                raise RuntimeError(f"invalid quoted {name} in {path} at line {line_number}")
            value = unquoted
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not value:
            raise RuntimeError(f"empty {name} in {path} at line {line_number}")
        return value
    return None


@dataclass(frozen=True)
class GatewayConfig:
    """Transport settings shared by proposal and verifier calls."""

    api_key: str | None = field(default=None, repr=False, compare=False)
    # With no explicit key, BugBunny checks this environment variable and then
    # MARTIAN_API_KEY. The CLI additionally opts into a local .env file.
    api_key_env: str | None = None
    # Compatible gateways sometimes encode basic-auth credentials or opaque
    # routing tokens in this URL. Treat the complete endpoint as sensitive.
    api_base: str | None = field(default=None, repr=False)
    dotenv_path: Path | None = field(default=None, repr=False, compare=False)
    timeout_seconds: float = 300
    max_retries: int = 1
    max_output_tokens: int = 32_768
    temperature: float = 0.0
    codex_executable: str = "codex"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not math.isfinite(self.temperature):
            raise ValueError("temperature must be finite")
        if not self.codex_executable:
            raise ValueError("codex_executable must not be empty")
        if self.api_key is not None and not self.api_key:
            raise ValueError("api_key must not be empty")
        if self.api_key_env is not None and not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty")

    def resolved_api_key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key
        if self.api_key_env and (value := os.environ.get(self.api_key_env)):
            return value
        if value := os.environ.get(MARTIAN_API_KEY_ENV):
            return value
        if self.dotenv_path is not None:
            return _dotenv_value(self.dotenv_path, MARTIAN_API_KEY_ENV)
        return None

    def effective_api_base(self) -> str:
        return self.api_base or MARTIAN_API_BASE

    def runtime_provenance(self, model: str) -> dict[str, Any]:
        """Return a reproducibility record without serializing credentials.

        This deliberately records only the API-base host plus a one-way hash of
        the complete configured value. Paths, queries, userinfo, API keys, and
        provider environment values never enter the returned mapping.
        """

        provider, _ = _model_parts(model)
        is_codex = provider == "codex"
        if is_codex:
            auth_mode = "codex_current_login"
            transport = "codex_cli"
            transport_version = _codex_cli_version(self.codex_executable)
        else:
            if self.api_key is not None:
                auth_mode = "explicit_api_key"
            elif self.api_key_env and os.environ.get(self.api_key_env):
                auth_mode = "configured_environment"
            elif os.environ.get(MARTIAN_API_KEY_ENV):
                auth_mode = "martian_environment"
            elif self.resolved_api_key() is not None:
                auth_mode = "martian_dotenv"
            else:
                auth_mode = "missing"
            transport = "martian_http"
            transport_version = _distribution_version("httpx")

        api_base = _api_base_provenance(None if is_codex else self.effective_api_base())
        return {
            "requested_model": model,
            "provider": provider,
            "transport": transport,
            "transport_version": transport_version,
            "auth_mode": auth_mode,
            "credential_configured": (
                False
                if is_codex
                else self.api_key is not None or self.resolved_api_key() is not None
            ),
            "limits": {
                "timeout_seconds": self.timeout_seconds,
                "max_retries": self.max_retries,
                "max_output_tokens": self.max_output_tokens,
                "max_output_tokens_transport_applied": not is_codex,
                "temperature": self.temperature,
                "temperature_applied": not is_codex and _martian_uses_temperature(model),
                "reasoning_effort_parameter_will_be_sent": (
                    is_codex or _martian_uses_reasoning_effort(model)
                ),
            },
            "api_base": api_base,
        }


@dataclass(frozen=True)
class GatewayResult:
    payload: dict[str, Any]
    call: CallRecord

    def __iter__(self):  # type: ignore[no-untyped-def]
        # Convenient without weakening the named, self-documenting API.
        yield self.payload
        yield self.call


@dataclass(frozen=True)
class _BackendResult:
    payload: dict[str, Any]
    resolved_model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    attempt_count: int = 1
    retry_errors: tuple[str, ...] = ()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _strict_parse_float(text: str) -> float:
    # parse_constant only intercepts the literal Infinity/NaN tokens; an
    # overflowing decimal literal such as 1e999 otherwise parses to inf and
    # bypasses the non-finite rejection.
    result = float(text)
    if not math.isfinite(result):
        raise ValueError(f"non-finite JSON number is not allowed: {text}")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key!r}")
        result[key] = value
    return result


def strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        parse_float=_strict_parse_float,
        object_pairs_hook=_reject_duplicate_keys,
    )


def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reject payloads that canonical hashing cannot represent.

    SDK-object payloads bypass the strict text parser, so non-finite numbers
    or non-JSON types would otherwise surface only as a bare ValueError from
    response hashing on the success path, outside the retryable taxonomy.
    """

    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResponseFormatError(f"model payload is not canonical JSON: {exc}") from exc
    return payload


def extract_json_object(value: Any) -> dict[str, Any]:
    """Extract one JSON object from SDK objects, text, or fenced fallback text."""

    if isinstance(value, Mapping):
        return _canonical_payload(dict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return _canonical_payload(dict(dumped))
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ResponseFormatError(f"model response was not valid UTF-8: {exc}") from exc
    if not isinstance(value, str):
        raise ResponseFormatError("model response did not contain JSON text")

    text = value.lstrip("\ufeff").strip()
    if not text:
        raise ResponseFormatError("model response was empty")

    candidates = [text]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    )
    errors: list[BaseException] = []
    for candidate in candidates:
        try:
            parsed = strict_json_loads(candidate)
        except (TypeError, ValueError) as exc:
            errors.append(exc)
        else:
            if isinstance(parsed, dict):
                return parsed

    # Some providers surround otherwise-valid JSON with a short explanation.
    # raw_decode finds the first complete object; schema validation later makes
    # choosing an unrelated incidental object fail closed.
    decoder = json.JSONDecoder(
        parse_constant=_reject_json_constant,
        parse_float=_strict_parse_float,
        object_pairs_hook=_reject_duplicate_keys,
    )
    for start, character in enumerate(text):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except (TypeError, ValueError) as exc:
            errors.append(exc)
            continue
        if isinstance(parsed, dict):
            return parsed
    detail = str(errors[-1]) if errors else "no JSON object found"
    raise ResponseFormatError(f"could not parse model JSON: {detail}")


def _schema_types(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return ()


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _pattern_search(pattern: str, value: str) -> bool:
    """Pattern search with ECMA end-anchor semantics.

    Python's ``$`` also matches before one trailing newline; ECMA's does not,
    and the Codex CLI enforces the same schemas natively with ECMA semantics.
    A final unescaped ``$`` is therefore checked as ``\\Z``.
    """

    if pattern.endswith("$"):
        stem = pattern[:-1]
        trailing_backslashes = len(stem) - len(stem.rstrip("\\"))
        if trailing_backslashes % 2 == 0:
            pattern = stem + r"\Z"
    return re.search(pattern, value) is not None


def _validate_json_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate the strict JSON-Schema subset used by BugBunny outputs."""

    types = _schema_types(schema.get("type"))
    if types and not any(_matches_type(value, kind) for kind in types):
        raise ResponseFormatError(f"{path} does not have required type {types}")
    if "enum" in schema and value not in schema["enum"]:
        raise ResponseFormatError(f"{path} is not an allowed enum value")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ResponseFormatError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ResponseFormatError(f"{path} has unknown properties: {', '.join(unknown)}")
        for key, item in value.items():
            subschema = properties.get(key)
            if isinstance(subschema, Mapping):
                _validate_json_schema(item, subschema, path=f"{path}.{key}")
    elif isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ResponseFormatError(f"{path} contains too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ResponseFormatError(f"{path} contains too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, path=f"{path}[{index}]")
    elif isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ResponseFormatError(f"{path} is shorter than minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ResponseFormatError(f"{path} is longer than maxLength")
        pattern = schema.get("pattern")
        # JSON-Schema pattern semantics are a search; schemas that mean a full
        # match anchor themselves.
        if isinstance(pattern, str) and not _pattern_search(pattern, value):
            raise ResponseFormatError(f"{path} does not match required pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not is_finite_number(value):
            raise ResponseFormatError(f"{path} must be finite")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ResponseFormatError(f"{path} is less than minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ResponseFormatError(f"{path} is greater than maximum")


def _member(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _content_text(content: Any) -> Any:
    if isinstance(content, (str, bytes, Mapping)):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for block in content:
            text = _member(block, "text")
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "".join(parts)
    return content


def _martian_payload(response: Any) -> dict[str, Any]:
    choices = _member(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        # A small number of adapters return the parsed object directly.
        if isinstance(response, Mapping) and "choices" not in response:
            return dict(response)
        raise ResponseFormatError("Martian response has no choices")
    message = _member(choices[0], "message")
    if message is None:
        raise ResponseFormatError("Martian response choice has no message")
    parsed = _member(message, "parsed")
    if parsed is not None:
        return extract_json_object(parsed)
    content = _member(message, "content")
    if content not in (None, ""):
        return extract_json_object(_content_text(content))
    tool_calls = _member(message, "tool_calls")
    if isinstance(tool_calls, Sequence) and tool_calls:
        function = _member(tool_calls[0], "function")
        arguments = _member(function, "arguments")
        if arguments is not None:
            return extract_json_object(arguments)
    finish_reason = _member(choices[0], "finish_reason")
    usage = _member(response, "usage")
    completion_tokens = _integer(_member(usage, "completion_tokens"))
    completion_details = _member(usage, "completion_tokens_details")
    reasoning_tokens = _integer(_member(completion_details, "reasoning_tokens"))
    raise ResponseFormatError(
        "Martian message has no JSON content "
        f"(finish_reason={finish_reason!r}, completion_tokens={completion_tokens!r}, "
        f"reasoning_tokens={reasoning_tokens!r})"
    )


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
        except OverflowError:
            return None
        if math.isfinite(result):
            return result
    return None


def _usage(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = _member(response, "usage")
    if usage is None:
        return None, None, None
    input_tokens = _integer(_member(usage, "prompt_tokens"))
    if input_tokens is None:
        input_tokens = _integer(_member(usage, "input_tokens"))
    output_tokens = _integer(_member(usage, "completion_tokens"))
    if output_tokens is None:
        output_tokens = _integer(_member(usage, "output_tokens"))
    details = _member(usage, "prompt_tokens_details")
    cached = _integer(_member(details, "cached_tokens")) if details is not None else None
    if cached is None:
        cached = _integer(_member(usage, "cache_read_input_tokens"))
    return input_tokens, output_tokens, cached


def _response_cost(response: Any) -> float | None:
    hidden = _member(response, "_hidden_params")
    for value in (
        _member(hidden, "response_cost") if hidden is not None else None,
        _member(response, "response_cost"),
        _member(_member(response, "usage"), "cost"),
    ):
        result = _finite_float(value)
        if result is not None and result >= 0:
            return result
    return None


def _structured_output_unsupported(error: BaseException) -> bool:
    message = str(error).casefold()
    mentions_feature = any(
        marker in message for marker in ("response_format", "json_schema", "structured output")
    )
    rejects_feature = any(
        marker in message
        for marker in (
            "unsupported",
            "not support",
            "unknown parameter",
            "invalid parameter",
            "not permitted",
        )
    )
    return mentions_feature and rejects_feature


_MAX_RESPONSE_BODY_BYTES = 64 * 1024 * 1024


async def _read_bounded_body(response: httpx.Response) -> bytes:
    """Read a response body with a hard size cap.

    Full buffering would otherwise hand a hostile or misconfigured endpoint a
    memory-exhaustion lever: the model-side completion cap bounds tokens, not
    the bytes an arbitrary HTTP server chooses to stream back.
    """

    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > _MAX_RESPONSE_BODY_BYTES:
            raise ResponseFormatError(
                f"model response body exceeded {_MAX_RESPONSE_BODY_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _martian_http_error(status_code: int, body: bytes) -> RuntimeError:
    message = "request failed"
    try:
        parsed = extract_json_object(body)
        error = parsed.get("error")
        candidate = _member(error, "message") if error is not None else None
        if isinstance(candidate, str) and candidate.strip():
            message = candidate.strip()[:1000]
    except (ResponseFormatError, UnicodeError, ValueError):
        pass
    return RuntimeError(f"Martian Gateway returned HTTP {status_code}: {message}")


def _martian_retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            parsed = float(retry_after)
        except ValueError:
            parsed = None
        # float() accepts "nan"/"inf" and NaN survives the min/max clamp, so a
        # non-finite header would become an unbounded asyncio.sleep holding a
        # global concurrency slot.
        if parsed is not None and math.isfinite(parsed):
            return min(max(parsed, 0.0), 30.0)
    return float(min(2**attempt, 8))


def _safe_error(error: BaseException, secrets: Sequence[str | None]) -> str:
    rendered = f"{type(error).__name__}: {error}"
    # Redact containers such as credential-bearing endpoint URLs before any
    # credential that may be a substring of them; otherwise the first
    # replacement can prevent the complete sensitive value from matching.
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        rendered = rendered.replace(secret, "[REDACTED]")
    rendered = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", rendered)
    rendered = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", rendered)
    rendered = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", rendered)
    return rendered[:2000]


def _credential_environment_values() -> tuple[str, ...]:
    """Return credentials that provider errors must never enter into artifacts."""

    credential_name = re.compile(
        r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|ACCESS_KEY)$",
        flags=re.IGNORECASE,
    )
    values: list[str] = []
    for name, value in os.environ.items():
        if value and credential_name.search(name):
            values.append(value)
    # Longest first prevents a short credential that is a substring of a
    # longer one from leaving a recognizable suffix behind.
    return tuple(sorted(set(values), key=len, reverse=True))


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_canonical_sha256(value: Mapping[str, Any]) -> str | None:
    """Hash provider output when possible without masking the original error."""

    try:
        return _canonical_sha256(value)
    except (TypeError, ValueError):
        return None


def _model_parts(model: str) -> tuple[str, str]:
    if not isinstance(model, str) or not model.strip() or "/" not in model:
        raise ValueError(
            "model must be provider-prefixed, for example openai/gpt-5.4-mini or codex/gpt-5.4-mini"
        )
    provider, resolved = model.split("/", 1)
    if not provider or not resolved or provider != provider.strip() or resolved != resolved.strip():
        raise ValueError("model provider and identifier must both be non-empty")
    return provider.casefold(), resolved


def _distribution_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _codex_cli_version(executable: str) -> str | None:
    """Read only a semantic version from ``codex --version`` output."""

    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", output)
    return match.group(0) if match else None


def _api_base_provenance(api_base: str | None) -> dict[str, Any]:
    if api_base is None:
        return {"configured": False, "host": None, "sha256": None}
    host: str | None = None
    try:
        parsed = urlsplit(api_base)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            host = parsed.hostname.casefold()
    except ValueError:
        # An invalid or unusual configured endpoint still gets a stable
        # fingerprint, but none of its raw content is persisted.
        pass
    return {
        "configured": True,
        "host": host,
        "sha256": hashlib.sha256(api_base.encode("utf-8")).hexdigest(),
    }


class ModelGateway:
    """One async JSON interface over Martian Gateway and current-login Codex CLI."""

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int | None = None,
    ):
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.config = config or GatewayConfig()
        self._http_client = http_client
        self._owns_http_client = False
        self.max_concurrency = max_concurrency
        self._request_semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency is not None else None
        )
        self._runtime_provenance_cache: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> ModelGateway:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            self._owns_http_client = False

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            keepalive = max(20, self.max_concurrency or 0)
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=keepalive),
                follow_redirects=False,
            )
            self._owns_http_client = True
        return self._http_client

    def runtime_provenance(self, model: str) -> dict[str, Any]:
        """Expose the config provenance API from the active gateway."""

        if model not in self._runtime_provenance_cache:
            self._runtime_provenance_cache[model] = self.config.runtime_provenance(model)
        # Return a JSON round-trip copy so callers cannot mutate the cache.
        return json.loads(json.dumps(self._runtime_provenance_cache[model]))

    async def complete_json(
        self,
        prompt: str,
        *,
        model: str,
        stage: str,
        schema_name: str,
        schema: Mapping[str, Any],
        chunk_id: str | None = None,
        reasoning_effort: str = "low",
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        queue_timeout_seconds: float | None = None,
        operation_timeout_seconds: float | None = None,
    ) -> GatewayResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        provider, resolved_request = self._model_parts(model)
        if not stage.strip():
            raise ValueError("stage must not be empty")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", schema_name):
            raise ValueError("schema_name must be a simple 1-64 character identifier")
        if not isinstance(schema, Mapping):
            raise TypeError("schema must be a mapping")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", reasoning_effort):
            raise ValueError("reasoning_effort contains unsupported characters")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if queue_timeout_seconds is not None and (
            not isinstance(queue_timeout_seconds, (int, float))
            or isinstance(queue_timeout_seconds, bool)
            or not math.isfinite(queue_timeout_seconds)
            or queue_timeout_seconds <= 0
        ):
            raise ValueError("queue_timeout_seconds must be finite and positive")
        if operation_timeout_seconds is not None and (
            not isinstance(operation_timeout_seconds, (int, float))
            or isinstance(operation_timeout_seconds, bool)
            or not math.isfinite(operation_timeout_seconds)
            or operation_timeout_seconds <= 0
        ):
            raise ValueError("operation_timeout_seconds must be finite and positive")
        if provider != "codex" and reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("Martian reasoning_effort must be low, medium, or high")
        effective_max_output_tokens = max_output_tokens or self.config.max_output_tokens

        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "max_output_tokens_planning": effective_max_output_tokens,
                    "max_output_tokens_transport_applied": provider != "codex",
                    "schema_name": schema_name,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        schema_sha256 = _canonical_sha256(dict(schema))
        gateway = "codex_cli" if provider == "codex" else "martian_http"
        api_key = None if provider == "codex" else self.config.resolved_api_key()
        backend: _BackendResult | None = None
        started = time.monotonic()
        semaphore_acquired = False
        try:
            if self._request_semaphore is not None:
                try:
                    if queue_timeout_seconds is None:
                        await self._request_semaphore.acquire()
                    else:
                        await acquire_semaphore_bounded(
                            self._request_semaphore, queue_timeout_seconds
                        )
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"model request queue wait exceeded {queue_timeout_seconds:g}s"
                    ) from exc
                semaphore_acquired = True

            async def invoke() -> _BackendResult:
                if provider == "codex":
                    return await self._complete_codex(
                        prompt,
                        model=resolved_request,
                        schema=schema,
                        reasoning_effort=reasoning_effort,
                        system_prompt=system_prompt,
                    )
                if api_key is None:
                    raise RuntimeError(
                        "Martian API key is not configured; set MARTIAN_API_KEY, "
                        "add it to .env, or pass --api-key-env"
                    )
                return await self._complete_martian(
                    prompt,
                    model=model,
                    schema_name=schema_name,
                    schema=schema,
                    reasoning_effort=reasoning_effort,
                    system_prompt=system_prompt,
                    api_key=api_key,
                    max_output_tokens=effective_max_output_tokens,
                )

            if operation_timeout_seconds is None:
                backend = await invoke()
            else:
                try:
                    async with asyncio.timeout(operation_timeout_seconds):
                        backend = await invoke()
                except TimeoutError as exc:
                    raise TimeoutError(
                        f"model request execution exceeded {operation_timeout_seconds:g}s"
                    ) from exc
        except Exception as exc:
            if backend is None and isinstance(exc, _BackendFailure):
                backend = exc.backend
            safe = _safe_error(
                exc,
                (
                    *_credential_environment_values(),
                    api_key,
                    self.config.api_key,
                    self.config.api_base,
                ),
            )
            call = CallRecord(
                stage=stage,
                gateway=gateway,
                requested_model=model,
                resolved_model=backend.resolved_model if backend else None,
                latency_ms=round((time.monotonic() - started) * 1000),
                chunk_id=chunk_id,
                input_tokens=backend.input_tokens if backend else None,
                output_tokens=backend.output_tokens if backend else None,
                cached_input_tokens=backend.cached_input_tokens if backend else None,
                cost_usd=backend.cost_usd if backend else None,
                response_sha256=(
                    exc.response_sha256
                    if isinstance(exc, _BackendFailure) and exc.response_sha256 is not None
                    else _optional_canonical_sha256(backend.payload)
                    if backend
                    else None
                ),
                request_sha256=request_sha256,
                schema_sha256=schema_sha256,
                error=safe,
                attempt_count=int(getattr(exc, "attempt_count", 1)),
                retry_errors=tuple(getattr(exc, "retry_errors", ())),
            )
            raise GatewayError(safe, call) from exc
        finally:
            if self._request_semaphore is not None and semaphore_acquired:
                self._request_semaphore.release()

        call = CallRecord(
            stage=stage,
            gateway=gateway,
            requested_model=model,
            resolved_model=backend.resolved_model,
            latency_ms=round((time.monotonic() - started) * 1000),
            chunk_id=chunk_id,
            input_tokens=backend.input_tokens,
            output_tokens=backend.output_tokens,
            cached_input_tokens=backend.cached_input_tokens,
            cost_usd=backend.cost_usd,
            response_sha256=_canonical_sha256(backend.payload),
            request_sha256=request_sha256,
            schema_sha256=schema_sha256,
            attempt_count=backend.attempt_count,
            retry_errors=backend.retry_errors,
        )
        return GatewayResult(payload=backend.payload, call=call)

    @staticmethod
    def _model_parts(model: str) -> tuple[str, str]:
        return _model_parts(model)

    async def _complete_martian(
        self,
        prompt: str,
        *,
        model: str,
        schema_name: str,
        schema: Mapping[str, Any],
        reasoning_effort: str,
        system_prompt: str | None,
        api_key: str,
        max_output_tokens: int,
    ) -> _BackendResult:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
            "martian_metadata": {
                "application": "bugbunny",
                "schema_name": schema_name,
            },
        }
        if _martian_uses_reasoning_effort(model):
            request["reasoning_effort"] = reasoning_effort
        if _martian_uses_temperature(model):
            request["temperature"] = self.config.temperature

        active_request = request
        total_attempts = 0
        retry_errors: list[str] = []
        total_input_tokens: int | None = None
        total_output_tokens: int | None = None
        total_cached_tokens: int | None = None
        total_cost: float | None = None

        def add_integer(total: int | None, value: int | None) -> int | None:
            return (total or 0) + value if value is not None else total

        def add_float(total: float | None, value: float | None) -> float | None:
            return (total or 0.0) + value if value is not None else total

        for structured_attempt in range(self.config.max_retries + 1):
            response, body, attempt_count, transport_errors = await self._post_martian(
                active_request, api_key=api_key
            )
            total_attempts += attempt_count
            retry_errors.extend(transport_errors)
            if response.status_code >= 400:
                error = _martian_http_error(response.status_code, body)
                if _structured_output_unsupported(error):
                    fallback_request = dict(active_request)
                    fallback_request.pop("response_format", None)
                    (
                        fallback,
                        fallback_body,
                        fallback_attempts,
                        fallback_errors,
                    ) = await self._post_martian(fallback_request, api_key=api_key)
                    active_request = fallback_request
                    response = fallback
                    body = fallback_body
                    total_attempts += fallback_attempts
                    retry_errors.extend(fallback_errors)
                if response.status_code >= 400:
                    failure = _martian_http_error(response.status_code, body)
                    raise _BackendFailure(failure, total_attempts, retry_errors) from failure

            response_data: dict[str, Any] | None = None
            candidate: _BackendResult | None = None
            try:
                response_data = extract_json_object(body)
                input_tokens, output_tokens, cached_tokens = _usage(response_data)
                total_input_tokens = add_integer(total_input_tokens, input_tokens)
                total_output_tokens = add_integer(total_output_tokens, output_tokens)
                total_cached_tokens = add_integer(total_cached_tokens, cached_tokens)
                total_cost = add_float(total_cost, _response_cost(response_data))
                resolved_model = _member(response_data, "model")
                if not isinstance(resolved_model, str) or not resolved_model:
                    resolved_model = model
                payload = _martian_payload(response_data)
                candidate = _BackendResult(
                    payload=payload,
                    resolved_model=resolved_model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    cached_input_tokens=total_cached_tokens,
                    cost_usd=total_cost,
                    attempt_count=total_attempts,
                    retry_errors=tuple(retry_errors),
                )
                _validate_json_schema(candidate.payload, schema)
            except ResponseFormatError as exc:
                safe_error = _safe_error(exc, self._error_secrets(api_key))
                retry_errors.append(safe_error)
                if structured_attempt < self.config.max_retries:
                    continue
                failure_backend = candidate
                if failure_backend is None and response_data is not None:
                    resolved = _member(response_data, "model")
                    failure_backend = _BackendResult(
                        payload={},
                        resolved_model=(
                            resolved if isinstance(resolved, str) and resolved else model
                        ),
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        cached_input_tokens=total_cached_tokens,
                        cost_usd=total_cost,
                        attempt_count=total_attempts,
                        retry_errors=tuple(retry_errors),
                    )
                raise _BackendFailure(
                    exc,
                    total_attempts,
                    retry_errors,
                    backend=failure_backend,
                    response_sha256=hashlib.sha256(body).hexdigest(),
                ) from exc
            assert candidate is not None
            return candidate

        raise AssertionError("structured-output retry loop did not resolve")

    def _error_secrets(self, api_key: str | None = None) -> tuple[str | None, ...]:
        """The complete secret set every persisted error string passes through.

        Retry errors are stored in CallRecords even for eventually successful
        calls, so they must be redacted with the same strength as the
        top-level failure path rather than a narrower ad-hoc subset.
        """

        return (
            *_credential_environment_values(),
            api_key,
            self.config.api_key,
            self.config.api_base,
        )

    async def _post_martian(
        self,
        request: Mapping[str, Any],
        *,
        api_key: str,
    ) -> tuple[httpx.Response, bytes, int, tuple[str, ...]]:
        endpoint = f"{self.config.effective_api_base().rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"BugBunny/{__version__}",
        }
        last_error: BaseException | None = None
        retry_errors: list[str] = []
        for attempt in range(self.config.max_retries + 1):
            try:
                client = self._client()
                http_request = client.build_request(
                    "POST", endpoint, headers=headers, json=dict(request)
                )
                response = await client.send(http_request, stream=True)
                try:
                    body = await _read_bounded_body(response)
                finally:
                    await response.aclose()
            except httpx.TransportError as exc:
                last_error = exc
                retry_errors.append(_safe_error(exc, self._error_secrets(api_key)))
                if attempt >= self.config.max_retries:
                    raise _BackendFailure(exc, attempt + 1, retry_errors) from exc
                await asyncio.sleep(min(2**attempt, 8))
                continue
            except ResponseFormatError as exc:
                # An over-cap body is a persistent server pathology; spending
                # the retry budget re-downloading it would repeat the exposure.
                raise _BackendFailure(exc, attempt + 1, retry_errors) from exc
            if response.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
                return response, body, attempt + 1, tuple(retry_errors)
            if attempt >= self.config.max_retries:
                return response, body, attempt + 1, tuple(retry_errors)
            retry_errors.append(f"HTTP {response.status_code}")
            await asyncio.sleep(_martian_retry_delay(response, attempt))
        assert last_error is not None
        raise _BackendFailure(last_error, self.config.max_retries + 1, retry_errors)

    async def _complete_codex(
        self,
        prompt: str,
        *,
        model: str,
        schema: Mapping[str, Any],
        reasoning_effort: str,
        system_prompt: str | None,
    ) -> _BackendResult:
        combined_prompt = f"{system_prompt.rstrip()}\n\n{prompt}" if system_prompt else prompt
        last_error: BaseException | None = None
        failure_backend: _BackendResult | None = None
        failure_sha256: str | None = None
        retry_errors: list[str] = []
        for attempt in range(self.config.max_retries + 1):
            try:
                result, response_sha256 = await self._codex_attempt(
                    combined_prompt,
                    model=model,
                    schema=schema,
                    reasoning_effort=reasoning_effort,
                )
            except _BackendFailure as exc:
                last_error = exc.error
                failure_backend = exc.backend
                failure_sha256 = exc.response_sha256
                retry_errors.append(_safe_error(exc.error, self._error_secrets()))
                continue
            except (OSError, TimeoutError, ResponseFormatError, RuntimeError) as exc:
                last_error = exc
                failure_backend = None
                failure_sha256 = None
                retry_errors.append(_safe_error(exc, self._error_secrets()))
                continue
            try:
                _validate_json_schema(result.payload, schema)
            except ResponseFormatError as exc:
                last_error = exc
                failure_backend = replace(result, payload={})
                failure_sha256 = response_sha256
                retry_errors.append(_safe_error(exc, self._error_secrets()))
                continue
            return _BackendResult(
                payload=result.payload,
                resolved_model=result.resolved_model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                cost_usd=result.cost_usd,
                attempt_count=attempt + 1,
                retry_errors=tuple(retry_errors),
            )
        assert last_error is not None
        raise _BackendFailure(
            last_error,
            self.config.max_retries + 1,
            retry_errors,
            backend=failure_backend,
            response_sha256=failure_sha256,
        ) from last_error

    async def _codex_attempt(
        self,
        prompt: str,
        *,
        model: str,
        schema: Mapping[str, Any],
        reasoning_effort: str,
    ) -> tuple[_BackendResult, str]:
        with tempfile.TemporaryDirectory(prefix="bugbunny-codex-") as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            schema_path = root / "output-schema.json"
            output_path = root / "last-message.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False, sort_keys=True, allow_nan=False),
                encoding="utf-8",
            )
            command = [
                self.config.codex_executable,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--json",
                "--output-last-message",
                str(output_path),
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{reasoning_effort}"',
                "--color",
                "never",
            ]
            for feature in _CODEX_DISABLED_FEATURES:
                command.extend(("--disable", feature))
            command.append("-")
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(work),
                env=_codex_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=prompt.encode("utf-8")),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError(
                    f"codex exec timed out after {self.config.timeout_seconds:g}s"
                ) from None
            except asyncio.CancelledError:
                # Cancellation of the awaiting task must not orphan the agent
                # subprocess.
                process.kill()
                await process.wait()
                raise
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                if not detail:
                    detail = stdout.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"codex exec exited with status {process.returncode}: {detail[:2000]}"
                )

            # Usage, resolved model, and the response fingerprint are parsed
            # before payload extraction so a malformed final message still
            # surfaces the telemetry the Martian path preserves on failure.
            events = _codex_events(stdout)
            input_tokens, output_tokens, cached_tokens = _codex_usage(events)
            resolved_model = _codex_model(events) or model
            output = ""
            if output_path.is_file():
                output = output_path.read_text(encoding="utf-8")
            try:
                if not output:
                    output = _last_codex_message(events)
                payload = extract_json_object(output)
            except ResponseFormatError as exc:
                raise _BackendFailure(
                    exc,
                    1,
                    (),
                    backend=_BackendResult(
                        payload={},
                        resolved_model=resolved_model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_input_tokens=cached_tokens,
                        cost_usd=None,
                    ),
                    response_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                ) from exc
            backend = _BackendResult(
                payload=payload,
                resolved_model=resolved_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_tokens,
                # ChatGPT-authenticated Codex execution has no attributable
                # per-call API charge. None is more exact than an invented cost.
                cost_usd=None,
            )
            return backend, hashlib.sha256(output.encode("utf-8")).hexdigest()


def _codex_events(stdout: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = strict_json_loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _last_codex_message(events: Sequence[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        item = event.get("item")
        if not isinstance(item, Mapping):
            continue
        if item.get("type") not in {"agent_message", "message"}:
            continue
        for key in ("text", "content"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    raise ResponseFormatError("codex exec did not produce a final message")


def _codex_usage(
    events: Sequence[Mapping[str, Any]],
) -> tuple[int | None, int | None, int | None]:
    # turn.completed reports cumulative usage for the completed turn, so the
    # last usage object is authoritative rather than a sum of event snapshots.
    for event in reversed(events):
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        input_tokens = _integer(usage.get("input_tokens"))
        if input_tokens is None:
            input_tokens = _integer(usage.get("prompt_tokens"))
        output_tokens = _integer(usage.get("output_tokens"))
        if output_tokens is None:
            output_tokens = _integer(usage.get("completion_tokens"))
        cached = _integer(usage.get("cached_input_tokens"))
        if cached is None:
            cached = _integer(usage.get("cache_read_input_tokens"))
        return input_tokens, output_tokens, cached
    return None, None, None


def _codex_model(events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in reversed(events):
        for container in (event, event.get("turn"), event.get("item")):
            if isinstance(container, Mapping):
                model = container.get("model")
                if isinstance(model, str) and model:
                    return model
    return None


# Backward-friendly descriptive alias; both names refer to the same async API.
LLMGateway = ModelGateway
