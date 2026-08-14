"""Offline tests for configurable database business-smoke safety."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings
from scripts import database_smoke_support as support
from scripts import smoke_model_assisted_database_corpus as corpus_smoke
from scripts import smoke_session_close_memory as memory_smoke
from scripts.database_smoke_support import (
    BusinessOutcome,
    ExpectedSmokeState,
    SmokeExitCode,
    SmokeOptions,
    SmokeResult,
    build_parser,
    create_smoke_case,
    parse_options,
    render_result,
    run_database_smoke,
    validate_postgres_database_url,
)
from storage.database import dispose_engine


def test_shared_cli_defaults_to_sqlite_and_keeps_old_ascii_flag() -> None:
    """Both scripts should retain their old command while sharing one default."""

    parser = build_parser("test")

    assert parse_options(parser, []) == SmokeOptions(database="sqlite")
    assert parse_options(parser, ["--ascii"]) == SmokeOptions(
        database="sqlite",
        ascii_output=True,
    )
    assert parse_options(parser, ["--database", "sqlite", "--json"]) == SmokeOptions(
        database="sqlite",
        json_output=True,
    )


@pytest.mark.parametrize(
    ("module", "entrypoint"),
    [
        (corpus_smoke, corpus_smoke.main),
        (memory_smoke, memory_smoke.main),
    ],
    ids=("corpus", "session-close-memory"),
)
@pytest.mark.parametrize("argv", [[], ["--ascii"], ["--database", "sqlite"]])
def test_both_script_entrypoints_select_sqlite_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: ModuleType,
    entrypoint: Callable[[Sequence[str] | None], int],
    argv: list[str],
) -> None:
    """Default, legacy, and explicit SQLite commands should select SQLite."""

    selected: list[SmokeOptions] = []

    async def fake_run_database_smoke(**kwargs: object) -> tuple[SmokeResult, SmokeExitCode]:
        options = cast(SmokeOptions, kwargs["options"])
        selected.append(options)
        return (
            SmokeResult(
                success=True,
                database_backend=options.database,
                database_driver="sqlite+aiosqlite",
                user_id="smoke-user",
                session_id="smoke-session",
                run_id="run-id",
            ),
            SmokeExitCode.SUCCESS,
        )

    monkeypatch.setattr(module, "run_database_smoke", fake_run_database_smoke)

    assert entrypoint(argv) == 0
    assert selected == [
        SmokeOptions(
            database="sqlite",
            ascii_output="--ascii" in argv,
        )
    ]
    assert "database_backend=sqlite" in capsys.readouterr().out


def test_unique_smoke_ids_are_safe_for_repeated_runs() -> None:
    """Repeated invocations should not reuse user or session identifiers."""

    first = create_smoke_case("repeatable")
    second = create_smoke_case("repeatable")

    assert first.run_id != second.run_id
    assert first.user_id != second.user_id
    assert first.session_id != second.session_id


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///:memory:",
        "postgresql://user:secret@localhost/app",
        "postgresql+psycopg://user:secret@localhost/app",
    ],
)
def test_postgres_mode_rejects_non_asyncpg_urls_without_leaking_url(
    database_url: str,
) -> None:
    """The smoke should reject the wrong driver before an engine is created."""

    with pytest.raises(ValueError) as caught:
        validate_postgres_database_url(database_url)

    assert database_url not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_postgres_url_rejects_placeholder_and_accepts_asyncpg() -> None:
    """The placeholder must fail without forbidding external authentication."""

    placeholder = (
        "postgresql+asyncpg://psych_agent:replace-me@localhost/psych_agent"
    )
    valid = "postgresql+asyncpg://user:local-secret@localhost/psych_agent"
    passwordless = "postgresql+asyncpg://user@localhost/psych_agent"

    with pytest.raises(ValueError, match="not explicitly configured"):
        validate_postgres_database_url(placeholder)
    assert validate_postgres_database_url(valid) == valid
    assert validate_postgres_database_url(passwordless) == passwordless


@pytest.mark.asyncio
async def test_unconfigured_postgres_fails_before_engine_or_business_flow() -> None:
    """Placeholder settings must fail safely without connecting or falling back."""

    engine_called = False
    business_called = False

    def forbidden_engine_factory(settings: Settings) -> AsyncEngine:
        nonlocal engine_called
        engine_called = True
        raise AssertionError(settings.database_url)

    async def forbidden_business_flow(*args: object) -> BusinessOutcome:
        nonlocal business_called
        business_called = True
        raise AssertionError(args)

    result, exit_code = await run_database_smoke(
        smoke_name="configuration_failure",
        options=SmokeOptions(database="postgres"),
        expected=ExpectedSmokeState(
            session_status="active",
            long_term_memories_rows=0,
        ),
        business_flow=forbidden_business_flow,
        settings=Settings(_env_file=None),
        engine_factory=forbidden_engine_factory,
    )

    assert exit_code is SmokeExitCode.CONFIGURATION
    assert result.success is False
    assert result.database_backend == "postgres"
    assert result.cleanup_applied is False
    assert engine_called is False
    assert business_called is False
    assert "replace-me" not in (result.error_message_safe or "")
    assert "postgresql+asyncpg://" not in (result.error_message_safe or "")


@pytest.mark.asyncio
async def test_postgres_branch_uses_settings_without_creating_schema_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL selection should pass DATABASE_URL through and perform no DDL."""

    configured_url = (
        "postgresql+asyncpg://smoke:local-secret@localhost/psych_agent_acceptance"
    )
    received_urls: list[str] = []

    def isolated_engine_factory(settings: Settings) -> AsyncEngine:
        received_urls.append(settings.database_url)
        return create_async_engine("sqlite+aiosqlite:///:memory:")

    async def skip_real_schema_check(engine: AsyncEngine) -> None:
        assert engine.url.drivername == "sqlite+aiosqlite"

    monkeypatch.setattr(support, "_require_migrated_postgres_schema", skip_real_schema_check)
    runtime = await support._open_database_runtime(
        smoke_name="postgres_path",
        options=SmokeOptions(database="postgres"),
        case=create_smoke_case("postgres_path"),
        settings=Settings(database_url=configured_url, _env_file=None),
        engine_factory=isolated_engine_factory,
    )
    try:
        async with runtime.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        assert received_urls == [configured_url]
        assert runtime.database_path is None
        assert table_names == []
    finally:
        await dispose_engine(runtime.engine)


def test_result_json_is_machine_readable_and_contains_no_connection_secret() -> None:
    """Both output modes should render only the shared safe result object."""

    result = SmokeResult(
        success=False,
        database_backend="postgres",
        database_driver="postgresql+asyncpg",
        user_id="smoke-user",
        session_id="smoke-session",
        run_id="safe-run",
        error_type="SmokeConfigurationError",
        error_message_safe="DATABASE_URL is not explicitly configured",
    )

    rendered_json = render_result(result, json_output=True)
    rendered_ascii = render_result(result, json_output=False)

    assert json.loads(rendered_json)["database_backend"] == "postgres"
    assert "success=False" in rendered_ascii
    for forbidden in ("local-secret", "postgresql+asyncpg://", "Authorization"):
        assert forbidden not in rendered_json
        assert forbidden not in rendered_ascii


def test_exit_codes_and_destructive_operation_guards_are_stable() -> None:
    """Exit values and source-level destructive-operation guards should stay fixed."""

    assert [int(code) for code in SmokeExitCode] == [0, 2, 3, 4, 5, 6, 7, 8]

    project_root = Path(__file__).resolve().parents[2]
    script_paths = (
        project_root / "scripts" / "database_smoke_support.py",
        project_root / "scripts" / "smoke_model_assisted_database_corpus.py",
        project_root / "scripts" / "smoke_session_close_memory.py",
    )
    sources = [path.read_text(encoding="utf-8") for path in script_paths]

    assert all("drop_all" not in source for source in sources)
    assert all("TRUNCATE" not in source.upper() for source in sources)
    assert all("DELETE FROM" not in source.upper() for source in sources)
    assert "Base.metadata.create_all" not in sources[1]
    assert "Base.metadata.create_all" not in sources[2]

    repository_sources = [
        path.read_text(encoding="utf-8")
        for path in (project_root / "storage" / "repositories").glob("*.py")
    ]
    assert all(".commit(" not in source for source in repository_sources)
    assert all(".rollback(" not in source for source in repository_sources)
