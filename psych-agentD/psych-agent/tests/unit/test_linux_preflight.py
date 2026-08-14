"""Offline tests for Linux deployment preflight orchestration."""

import json
from pathlib import Path

from app.config import Settings
from scripts.check_database_connection import (
    CheckOptions as DatabaseCheckOptions,
)
from scripts.check_database_connection import DatabaseCheckResult, DatabaseExitCode
from scripts.check_llm_endpoint import CheckOptions as LLMCheckOptions
from scripts.check_llm_endpoint import EndpointCheckResult, EndpointExitCode
from scripts.check_redis_connection import (
    RedisCheckExitCode,
    RedisCheckOptions,
    RedisCheckResult,
)
from scripts.preflight_linux import (
    PreflightExitCode,
    PreflightOptions,
    parse_options,
    render_result,
    run_preflight,
)

SECRET = "preflight-secret-that-must-not-leak"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _settings(*, redis_required: bool = True) -> Settings:
    return Settings(
        app_env="production",
        app_runtime_mode="sqlalchemy_model",
        database_url=(
            f"postgresql+asyncpg://service:{SECRET}@database.internal/app"
        ),
        redis_url=f"redis://service:{SECRET}@redis.internal:6379/0",
        redis_required=redis_required,
        llm_base_url="https://llm.internal/v1",
        llm_api_key=SECRET,
        main_model_name="served-model",
        structured_model_name="served-model",
        safety_model_name="served-model",
        _env_file=None,
    )


async def test_preflight_reuses_all_three_check_modules_without_secret_output(
) -> None:
    """A successful deployment preflight should call each injected checker once."""

    calls: list[str] = []

    async def database_runner(
        settings: Settings,
        options: DatabaseCheckOptions,
    ) -> tuple[DatabaseCheckResult, DatabaseExitCode]:
        _ = (settings, options)
        calls.append("database")
        return (
            DatabaseCheckResult(
                url_env="DATABASE_URL",
                connection_status="ok",
                schema_status="ok",
                success=True,
            ),
            DatabaseExitCode.SUCCESS,
        )

    async def redis_runner(
        settings: Settings,
        options: RedisCheckOptions,
    ) -> tuple[RedisCheckResult, RedisCheckExitCode]:
        _ = (settings, options)
        calls.append("redis")
        return (
            RedisCheckResult(
                configured=True,
                required=True,
                ping_status="ok",
                success=True,
            ),
            RedisCheckExitCode.SUCCESS,
        )

    async def llm_runner(
        settings: Settings,
        options: LLMCheckOptions,
    ) -> tuple[EndpointCheckResult, EndpointExitCode]:
        _ = settings
        calls.append("llm")
        assert options.models_only is True
        return (
            EndpointCheckResult(
                provider_kind="unknown",
                base_url="<safe>",
                model="served-model",
                models_endpoint_status="ok",
                success=True,
            ),
            EndpointExitCode.SUCCESS,
        )

    result, exit_code = await run_preflight(
        _settings(),
        python_version=(3, 11, 9),
        project_root=PROJECT_ROOT,
        temporary_directory=PROJECT_ROOT / ".tmp",
        database_runner=database_runner,
        redis_runner=redis_runner,
        llm_runner=llm_runner,
    )

    rendered = render_result(result, json_output=True)
    assert exit_code is PreflightExitCode.SUCCESS
    assert result.success is True
    assert calls == ["database", "redis", "llm"]
    assert SECRET not in rendered
    assert "postgresql+asyncpg://" not in rendered
    assert "redis://" not in rendered
    assert "https://" not in rendered
    assert json.loads(rendered)["llm"]["status"] == "ok"


async def test_required_redis_failure_returns_stable_nonzero_code(
) -> None:
    """A failed required dependency must make deployment automation stop."""

    async def database_runner(
        settings: Settings,
        options: DatabaseCheckOptions,
    ) -> tuple[DatabaseCheckResult, DatabaseExitCode]:
        _ = (settings, options)
        return (
            DatabaseCheckResult(url_env="DATABASE_URL", success=True),
            DatabaseExitCode.SUCCESS,
        )

    async def redis_runner(
        settings: Settings,
        options: RedisCheckOptions,
    ) -> tuple[RedisCheckResult, RedisCheckExitCode]:
        _ = (settings, options)
        return (
            RedisCheckResult(
                configured=True,
                required=True,
                ping_status="unavailable",
            ),
            RedisCheckExitCode.CONNECTION,
        )

    async def llm_runner(
        settings: Settings,
        options: LLMCheckOptions,
    ) -> tuple[EndpointCheckResult, EndpointExitCode]:
        _ = (settings, options)
        return (
            EndpointCheckResult(
                provider_kind="unknown",
                base_url="<safe>",
                model="served-model",
                models_endpoint_status="ok",
            ),
            EndpointExitCode.SUCCESS,
        )

    result, exit_code = await run_preflight(
        _settings(),
        python_version=(3, 11, 9),
        project_root=PROJECT_ROOT,
        temporary_directory=PROJECT_ROOT / ".tmp",
        database_runner=database_runner,
        redis_runner=redis_runner,
        llm_runner=llm_runner,
    )

    assert exit_code is PreflightExitCode.REDIS
    assert result.redis.status == "unavailable"
    assert result.success is False


def test_preflight_cli_and_json_contract_are_stable() -> None:
    """The deployment CLI should expose only its human/JSON output switch."""

    assert parse_options(["--json"]) == PreflightOptions(json_output=True)
