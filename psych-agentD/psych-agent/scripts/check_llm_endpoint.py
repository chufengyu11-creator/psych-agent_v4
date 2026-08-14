"""Safely check one configured OpenAI-compatible LLM endpoint."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from llm.exceptions import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMNotFoundError,
    LLMOutputTruncatedError,
    LLMPermissionError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMRetryExhaustedError,
    LLMServerError,
    LLMTimeoutError,
)
from llm.local_client import OpenAICompatibleHTTPClient
from llm.retry_policy import RetryPolicy
from schemas.llm import LLMMessage, LLMMessageRole, LLMRequest


class EndpointExitCode(IntEnum):
    """Stable process exit codes for endpoint checks."""

    SUCCESS = 0
    CONFIGURATION = 2
    CONNECTION_OR_TIMEOUT = 3
    AUTH_OR_PERMISSION = 4
    MODEL_NOT_FOUND = 5
    PROTOCOL = 6
    JSON_VALIDATION = 7


@dataclass(frozen=True)
class CheckOptions:
    """Command options separated from argparse for deterministic tests."""

    json_output: bool = False
    skip_model_list: bool = False
    timeout_seconds: float | None = None
    structured_only: bool = False
    models_only: bool = False


@dataclass
class EndpointCheckResult:
    """Machine-readable endpoint check result without prompts or credentials."""

    provider_kind: str
    base_url: str
    model: str
    models_endpoint_status: str = "not_run"
    chat_endpoint_status: str = "not_run"
    json_output_status: str = "not_run"
    latency_ms: dict[str, int] = field(default_factory=dict)
    content_non_empty: bool = False
    json_parsed: bool = False
    api_call_count: int = 0
    success: bool = False
    error_type: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the stable command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--skip-model-list", action="store_true")
    parser.add_argument("--timeout", type=float, dest="timeout_seconds")
    parser.add_argument("--structured-only", action="store_true")
    parser.add_argument("--models-only", action="store_true")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> CheckOptions:
    """Parse CLI arguments into a testable options object."""

    args = build_parser().parse_args(argv)
    return CheckOptions(
        json_output=args.json_output,
        skip_model_list=args.skip_model_list,
        timeout_seconds=args.timeout_seconds,
        structured_only=args.structured_only,
        models_only=args.models_only,
    )


async def run_endpoint_checks(
    settings: Settings,
    options: CheckOptions,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_policy: RetryPolicy | None = None,
) -> tuple[EndpointCheckResult, EndpointExitCode]:
    """Run configured stages and return a sanitized result plus exit code."""

    result = EndpointCheckResult(
        provider_kind=provider_kind(settings.llm_base_url),
        base_url=safe_display_base_url(settings.llm_base_url),
        model=settings.structured_model_name.strip() or "<missing>",
    )
    total_started = monotonic()
    client: OpenAICompatibleHTTPClient | None = None
    try:
        if options.timeout_seconds is not None and options.timeout_seconds <= 0:
            msg = "timeout override must be greater than zero"
            raise LLMConfigurationError(msg)
        client = OpenAICompatibleHTTPClient.from_settings(
            settings,
            timeout_seconds=options.timeout_seconds,
            retry_policy=retry_policy,
            transport=transport,
        )
        result.base_url = client.base_url

        if options.models_only and (options.structured_only or options.skip_model_list):
            msg = "models-only cannot be combined with another model-list mode"
            raise LLMConfigurationError(msg)

        if options.models_only:
            model_check = await _check_models(client, result, options)
            if model_check is not None:
                return _finish(result, client, total_started, model_check)
            if result.models_endpoint_status != "ok":
                result.error_type = "ModelsEndpointUnsupported"
                return _finish(
                    result,
                    client,
                    total_started,
                    EndpointExitCode.PROTOCOL,
                )
            result.chat_endpoint_status = "skipped"
            result.json_output_status = "skipped"
            result.success = True
            return _finish(result, client, total_started, EndpointExitCode.SUCCESS)

        if options.structured_only:
            result.models_endpoint_status = "skipped"
            result.chat_endpoint_status = "skipped"
        else:
            model_check = await _check_models(client, result, options)
            if model_check is not None:
                return _finish(result, client, total_started, model_check)

            chat_error = await _check_chat(client, result, settings.structured_model_name)
            if chat_error is not None:
                return _finish(result, client, total_started, chat_error)

        json_error = await _check_json_output(
            client,
            result,
            settings.structured_model_name,
        )
        if json_error is not None:
            return _finish(result, client, total_started, json_error)

        result.success = True
        return _finish(result, client, total_started, EndpointExitCode.SUCCESS)
    except LLMError as exc:
        result.error_type = type(_root_error(exc)).__name__
        result.latency_ms["total"] = _elapsed_ms(total_started)
        return result, exit_code_for_error(exc)
    finally:
        if client is not None:
            result.api_call_count = client.request_count
            await client.aclose()


async def _check_models(
    client: OpenAICompatibleHTTPClient,
    result: EndpointCheckResult,
    options: CheckOptions,
) -> EndpointExitCode | None:
    """Check model discovery, tolerating an explicitly skipped or missing endpoint."""

    if options.skip_model_list:
        result.models_endpoint_status = "skipped"
        return None

    started = monotonic()
    try:
        model_ids = await client.list_models()
    except LLMNotFoundError:
        result.models_endpoint_status = "unsupported"
        return None
    except LLMError as exc:
        result.models_endpoint_status = "failed"
        result.error_type = type(_root_error(exc)).__name__
        return exit_code_for_error(exc)
    finally:
        result.latency_ms["models"] = _elapsed_ms(started)

    if result.model not in model_ids:
        result.models_endpoint_status = "model_not_found"
        result.error_type = "ConfiguredModelNotFound"
        return EndpointExitCode.MODEL_NOT_FOUND
    result.models_endpoint_status = "ok"
    return None


async def _check_chat(
    client: OpenAICompatibleHTTPClient,
    result: EndpointCheckResult,
    model_name: str,
) -> EndpointExitCode | None:
    """Check a minimal non-sensitive plain chat completion."""

    started = monotonic()
    try:
        response = await client.generate(
            LLMRequest(
                model_name=model_name,
                messages=[
                    LLMMessage(
                        role=LLMMessageRole.SYSTEM,
                        content="You are a connectivity test assistant.",
                    ),
                    LLMMessage(
                        role=LLMMessageRole.USER,
                        content="Reply with exactly: OK",
                    ),
                ],
                temperature=0.0,
                max_tokens=16,
            )
        )
    except LLMError as exc:
        result.chat_endpoint_status = "failed"
        result.error_type = type(_root_error(exc)).__name__
        return exit_code_for_error(exc)
    finally:
        result.latency_ms["chat"] = _elapsed_ms(started)

    result.content_non_empty = bool(response.content.strip())
    result.chat_endpoint_status = "ok" if result.content_non_empty else "failed"
    if not result.content_non_empty:
        result.error_type = "EmptyChatContent"
        return EndpointExitCode.PROTOCOL
    return None


async def _check_json_output(
    client: OpenAICompatibleHTTPClient,
    result: EndpointCheckResult,
    model_name: str,
) -> EndpointExitCode | None:
    """Check JSON mode with one tiny artificial payload."""

    started = monotonic()
    try:
        response = await client.generate(
            LLMRequest(
                model_name=model_name,
                messages=[
                    LLMMessage(
                        role=LLMMessageRole.USER,
                        content=(
                            "Return exactly one JSON object with this shape: "
                            '{"status":"ok"}. Do not include extra text.'
                        ),
                    )
                ],
                temperature=0.0,
                max_tokens=32,
                response_format="json_object",
            )
        )
    except LLMError as exc:
        result.json_output_status = "failed"
        result.error_type = type(_root_error(exc)).__name__
        return exit_code_for_error(exc)
    finally:
        result.latency_ms["json_output"] = _elapsed_ms(started)

    try:
        payload: object = json.loads(response.content)
    except json.JSONDecodeError:
        result.json_output_status = "failed"
        result.error_type = "JSONDecodeError"
        return EndpointExitCode.JSON_VALIDATION
    result.json_parsed = True
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        result.json_output_status = "failed"
        result.error_type = "JSONStatusMismatch"
        return EndpointExitCode.JSON_VALIDATION
    result.json_output_status = "ok"
    return None


def exit_code_for_error(error: LLMError) -> EndpointExitCode:
    """Map sanitized LLM failures to stable CLI exit codes."""

    root = _root_error(error)
    if isinstance(root, LLMConfigurationError):
        return EndpointExitCode.CONFIGURATION
    if isinstance(root, (LLMConnectionError, LLMTimeoutError, LLMRateLimitError, LLMServerError)):
        return EndpointExitCode.CONNECTION_OR_TIMEOUT
    if isinstance(root, (LLMAuthenticationError, LLMPermissionError)):
        return EndpointExitCode.AUTH_OR_PERMISSION
    if isinstance(root, LLMNotFoundError):
        return EndpointExitCode.MODEL_NOT_FOUND
    if isinstance(root, (LLMProtocolError, LLMOutputTruncatedError, LLMBadRequestError)):
        return EndpointExitCode.PROTOCOL
    if isinstance(root, LLMHTTPError):
        return EndpointExitCode.PROTOCOL
    return EndpointExitCode.PROTOCOL


def provider_kind(base_url: str) -> str:
    """Classify the endpoint for diagnostics only, never for client behavior."""

    try:
        hostname = (urlsplit(base_url).hostname or "").casefold()
    except ValueError:
        return "unknown"
    if hostname == "api.deepseek.com":
        return "deepseek"
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "openai-compatible-local"
    return "unknown"


def safe_display_base_url(base_url: str) -> str:
    """Return a normalized diagnostic URL without embedded credentials."""

    value = base_url.strip()
    if not value:
        return "<missing>"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-base-url>"
    if parsed.username is not None or parsed.password is not None:
        return "<redacted-base-url>"
    return value.rstrip("/")


def render_result(result: EndpointCheckResult, *, json_output: bool) -> str:
    """Render only non-secret status fields."""

    payload = asdict(result)
    if json_output:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return "\n".join(f"{key}={value}" for key, value in payload.items())


def main(argv: Sequence[str] | None = None) -> int:
    """Run endpoint checks from shared Settings and print sanitized status."""

    options = parse_options(argv)
    try:
        settings = Settings()
    except ValidationError:
        result = EndpointCheckResult(
            provider_kind="unknown",
            base_url="<invalid-settings>",
            model="<invalid-settings>",
            error_type="SettingsValidationError",
        )
        print(render_result(result, json_output=options.json_output))
        return int(EndpointExitCode.CONFIGURATION)
    result, exit_code = asyncio.run(run_endpoint_checks(settings, options))
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


def _finish(
    result: EndpointCheckResult,
    client: OpenAICompatibleHTTPClient,
    started: float,
    exit_code: EndpointExitCode,
) -> tuple[EndpointCheckResult, EndpointExitCode]:
    result.api_call_count = client.request_count
    result.latency_ms["total"] = _elapsed_ms(started)
    return result, exit_code


def _root_error(error: LLMError) -> LLMError:
    if isinstance(error, LLMRetryExhaustedError):
        return _root_error(error.last_error)
    return error


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))


if __name__ == "__main__":
    raise SystemExit(main())
