"""Check configured Redis connectivity using the formal shared infrastructure."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from runtime.redis_client import RedisClientFactory, RedisResource, create_redis_client
from runtime.redis_exceptions import (
    RedisAuthenticationError,
    RedisConfigurationError,
    RedisConnectionError,
    RedisPermissionError,
    RedisProtocolError,
    RedisTimeoutError,
    RedisUnavailableError,
)


class RedisCheckExitCode(IntEnum):
    """Stable outcomes for the standalone Redis check."""

    SUCCESS = 0
    CONFIGURATION = 2
    CONNECTION = 3
    AUTHORIZATION = 4
    TIMEOUT = 5
    PROTOCOL = 6


@dataclass(frozen=True)
class RedisCheckOptions:
    """Command options separated from argparse for unit tests."""

    json_output: bool = False
    timeout_seconds: float | None = None


@dataclass
class RedisCheckResult:
    """Safe connectivity result without credentials or a complete URL."""

    configured: bool
    required: bool
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    database: int | None = None
    tls: bool | None = None
    ping_status: str = "not_configured"
    latency_ms: int | None = None
    success: bool = False
    error_type: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone Redis check CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", type=float, dest="timeout_seconds")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> RedisCheckOptions:
    """Parse command arguments into a stable value object."""

    args = build_parser().parse_args(argv)
    return RedisCheckOptions(
        json_output=args.json_output,
        timeout_seconds=args.timeout_seconds,
    )


async def run_redis_check(
    settings: Settings,
    options: RedisCheckOptions,
    *,
    client_factory: RedisClientFactory | None = None,
) -> tuple[RedisCheckResult, RedisCheckExitCode]:
    """Create one formal Redis resource, PING once, and close it."""

    result = RedisCheckResult(
        configured=settings.redis_url is not None,
        required=settings.redis_required,
    )
    resource: RedisResource | None = None
    try:
        if options.timeout_seconds is not None and options.timeout_seconds <= 0:
            raise RedisConfigurationError("Redis check timeout must be positive")
        resource = create_redis_client(settings, client_factory=client_factory)
        if resource is None:
            result.error_type = "RedisNotConfigured"
            return result, RedisCheckExitCode.CONFIGURATION
        endpoint = resource.endpoint
        result.scheme = endpoint.scheme
        result.host = endpoint.host
        result.port = endpoint.port
        result.database = endpoint.database
        result.tls = endpoint.tls
        started = monotonic()
        try:
            await resource.ping(timeout_seconds=options.timeout_seconds)
        finally:
            result.latency_ms = max(0, round((monotonic() - started) * 1000))
        result.ping_status = "ok"
        result.success = True
        return result, RedisCheckExitCode.SUCCESS
    except RedisConfigurationError as exc:
        result.ping_status = "configuration_error"
        result.error_type = type(exc).__name__
        return result, RedisCheckExitCode.CONFIGURATION
    except (RedisAuthenticationError, RedisPermissionError) as exc:
        result.ping_status = "authorization_failed"
        result.error_type = type(exc).__name__
        return result, RedisCheckExitCode.AUTHORIZATION
    except RedisTimeoutError as exc:
        result.ping_status = "timeout"
        result.error_type = type(exc).__name__
        return result, RedisCheckExitCode.TIMEOUT
    except (RedisConnectionError, RedisUnavailableError) as exc:
        result.ping_status = "unavailable"
        result.error_type = type(exc).__name__
        return result, RedisCheckExitCode.CONNECTION
    except RedisProtocolError as exc:
        result.ping_status = "protocol_error"
        result.error_type = type(exc).__name__
        return result, RedisCheckExitCode.PROTOCOL
    finally:
        if resource is not None:
            await resource.aclose()


def render_result(result: RedisCheckResult, *, json_output: bool) -> str:
    """Render safe status fields in human or machine-readable form."""

    payload = asdict(result)
    if json_output:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return "\n".join(f"{key}={value}" for key, value in payload.items())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check without leaking invalid Settings values."""

    options = parse_options(argv)
    try:
        settings = Settings()
    except ValidationError:
        result = RedisCheckResult(
            configured=False,
            required=False,
            ping_status="configuration_error",
            error_type="SettingsValidationError",
        )
        print(render_result(result, json_output=options.json_output))
        return int(RedisCheckExitCode.CONFIGURATION)
    result, exit_code = asyncio.run(run_redis_check(settings, options))
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
