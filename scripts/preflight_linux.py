"""Run sanitized, read-only preflight checks for a Linux API deployment."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from scripts.check_database_connection import (
    CheckOptions as DatabaseCheckOptions,
)
from scripts.check_database_connection import (
    DatabaseCheckResult,
    DatabaseExitCode,
    run_database_checks,
)
from scripts.check_llm_endpoint import (
    CheckOptions as LLMCheckOptions,
)
from scripts.check_llm_endpoint import (
    EndpointCheckResult,
    EndpointExitCode,
    run_endpoint_checks,
)
from scripts.check_redis_connection import (
    RedisCheckExitCode,
    RedisCheckOptions,
    RedisCheckResult,
    run_redis_check,
)

MINIMUM_PYTHON = (3, 11)


class PreflightExitCode(IntEnum):
    """Stable aggregate exit codes for deployment automation."""

    SUCCESS = 0
    PYTHON_VERSION = 2
    SETTINGS = 3
    DIRECTORIES = 4
    DATABASE = 5
    REDIS = 6
    LLM = 7


@dataclass(frozen=True)
class PreflightOptions:
    """Command options separated from process-global parsing."""

    json_output: bool = False


@dataclass
class PreflightCheck:
    """One safe component outcome without endpoint or credential values."""

    required: bool
    status: str = "not_run"
    checker_exit_code: int | None = None


@dataclass
class DirectoryChecks:
    """Named directory capabilities without filesystem paths."""

    project_root_readable: bool = False
    migrations_readable: bool = False
    temporary_directory_writable: bool = False


@dataclass
class PreflightResult:
    """Machine-readable aggregate that is safe for deployment logs."""

    python_version: str
    python: PreflightCheck
    settings: PreflightCheck
    directories: PreflightCheck
    directory_checks: DirectoryChecks = field(default_factory=DirectoryChecks)
    database: PreflightCheck = field(
        default_factory=lambda: PreflightCheck(required=True)
    )
    redis: PreflightCheck = field(
        default_factory=lambda: PreflightCheck(required=True)
    )
    llm: PreflightCheck = field(
        default_factory=lambda: PreflightCheck(required=True)
    )
    success: bool = False


DatabaseRunner = Callable[
    [Settings, DatabaseCheckOptions],
    Awaitable[tuple[DatabaseCheckResult, DatabaseExitCode]],
]
RedisRunner = Callable[
    [Settings, RedisCheckOptions],
    Awaitable[tuple[RedisCheckResult, RedisCheckExitCode]],
]
LLMRunner = Callable[
    [Settings, LLMCheckOptions],
    Awaitable[tuple[EndpointCheckResult, EndpointExitCode]],
]


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small Linux preflight CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> PreflightOptions:
    """Parse CLI arguments into a testable value object."""

    args = build_parser().parse_args(argv)
    return PreflightOptions(json_output=args.json_output)


async def run_preflight(
    settings: Settings,
    *,
    python_version: tuple[int, int, int] | None = None,
    project_root: Path = PROJECT_ROOT,
    temporary_directory: Path | None = None,
    database_runner: DatabaseRunner = run_database_checks,
    redis_runner: RedisRunner = run_redis_check,
    llm_runner: LLMRunner = run_endpoint_checks,
) -> tuple[PreflightResult, PreflightExitCode]:
    """Run required checks sequentially through the existing check modules."""

    version = python_version or (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    python_check = _python_check(version)
    database_required = settings.app_runtime_mode != "in_memory"
    redis_required = settings.redis_required
    llm_required = settings.app_runtime_mode == "sqlalchemy_model"
    result = PreflightResult(
        python_version=".".join(str(part) for part in version),
        python=python_check,
        settings=PreflightCheck(required=True, status="ok"),
        directories=PreflightCheck(required=True),
        database=PreflightCheck(required=database_required),
        redis=PreflightCheck(required=redis_required),
        llm=PreflightCheck(required=llm_required),
    )

    if python_check.status != "ok":
        return result, PreflightExitCode.PYTHON_VERSION
    if database_required:
        database_result, database_code = await database_runner(
            settings,
            DatabaseCheckOptions(),
        )
        result.database.status = "ok" if database_result.success else "failed"
        result.database.checker_exit_code = int(database_code)
        if (
            not database_result.success
            and database_result.schema_status not in {"", "not_run"}
        ):
            result.database.status = database_result.schema_status
    else:
        result.database.status = "not_required"

    redis_result, redis_code = await redis_runner(settings, RedisCheckOptions())
    result.redis.status = redis_result.ping_status
    result.redis.checker_exit_code = int(redis_code)

    if llm_required:
        llm_result, llm_code = await llm_runner(
            settings,
            LLMCheckOptions(models_only=True),
        )
        result.llm.status = llm_result.models_endpoint_status
        result.llm.checker_exit_code = int(llm_code)
    else:
        result.llm.status = "not_required"

    directory_check, directory_details = check_directories(
        project_root=project_root,
        temporary_directory=temporary_directory,
    )
    result.directories = directory_check
    result.directory_checks = directory_details

    exit_code = _aggregate_exit_code(result)
    result.success = exit_code is PreflightExitCode.SUCCESS
    return result, exit_code


def check_directories(
    *,
    project_root: Path,
    temporary_directory: Path | None = None,
) -> tuple[PreflightCheck, DirectoryChecks]:
    """Check code resources and one scratch write without exposing paths."""

    migrations = project_root / "storage" / "migrations"
    scratch = temporary_directory or Path(tempfile.gettempdir())
    details = DirectoryChecks(
        project_root_readable=(
            project_root.is_dir() and os.access(project_root, os.R_OK)
        ),
        migrations_readable=(
            migrations.is_dir() and os.access(migrations, os.R_OK)
        ),
        temporary_directory_writable=_can_write_temporary_file(scratch),
    )
    status = "ok" if all(asdict(details).values()) else "failed"
    return PreflightCheck(required=True, status=status), details


def render_result(result: PreflightResult, *, json_output: bool) -> str:
    """Render statuses only, without URLs, endpoint metadata, or secrets."""

    payload = asdict(result)
    if json_output:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)
    lines = [
        f"python_version={result.python_version}",
        f"success={result.success}",
    ]
    for name in ("python", "settings", "directories", "database", "redis", "llm"):
        check = getattr(result, name)
        lines.extend(
            (
                f"{name}.required={check.required}",
                f"{name}.status={check.status}",
                f"{name}.checker_exit_code={check.checker_exit_code}",
            )
        )
    lines.extend(
        f"directories.{name}={value}"
        for name, value in asdict(result.directory_checks).items()
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Load shared Settings and return a stable preflight exit code."""

    options = parse_options(argv)
    version = (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    python_check = _python_check(version)
    if python_check.status != "ok":
        result = _early_failure_result(version, python_check, "not_run")
        print(render_result(result, json_output=options.json_output))
        return int(PreflightExitCode.PYTHON_VERSION)
    try:
        settings = Settings()
    except ValidationError:
        result = _early_failure_result(version, python_check, "failed")
        print(render_result(result, json_output=options.json_output))
        return int(PreflightExitCode.SETTINGS)

    result, exit_code = asyncio.run(run_preflight(settings, python_version=version))
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


def _python_check(version: tuple[int, int, int]) -> PreflightCheck:
    status = "ok" if version[:2] >= MINIMUM_PYTHON else "unsupported"
    return PreflightCheck(required=True, status=status)


def _can_write_temporary_file(directory: Path) -> bool:
    if not directory.is_dir() or not os.access(directory, os.W_OK):
        return False
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".psych-agent-preflight-",
            dir=directory,
        ):
            pass
    except OSError:
        return False
    return True


def _aggregate_exit_code(result: PreflightResult) -> PreflightExitCode:
    if result.database.required and result.database.status != "ok":
        return PreflightExitCode.DATABASE
    if result.redis.required and result.redis.status != "ok":
        return PreflightExitCode.REDIS
    if result.llm.required and result.llm.status != "ok":
        return PreflightExitCode.LLM
    if result.directories.status != "ok":
        return PreflightExitCode.DIRECTORIES
    return PreflightExitCode.SUCCESS


def _early_failure_result(
    version: tuple[int, int, int],
    python_check: PreflightCheck,
    settings_status: str,
) -> PreflightResult:
    return PreflightResult(
        python_version=".".join(str(part) for part in version),
        python=python_check,
        settings=PreflightCheck(required=True, status=settings_status),
        directories=PreflightCheck(required=True),
    )


if __name__ == "__main__":
    raise SystemExit(main())
