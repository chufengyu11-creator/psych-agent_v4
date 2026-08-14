"""Run two artificial turns through the real model-assisted SQLAlchemy runtime."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.feedback_evaluator import FeedbackEvaluator
from agents.output_guard import OutputGuard
from agents.response_agent import ResponseAgent
from app.config import Settings
from llm.client import ManagedLLMClient
from llm.exceptions import LLMConfigurationError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.retry_policy import RetryPolicy
from llm.structured_client import StructuredLLMClient, StructuredLLMClientProtocol
from runtime.application import ApplicationRuntime, RuntimeConfigurationError
from runtime.factory import build_application_runtime
from schemas.common import SessionId, UserId
from schemas.messages import ChatTurnResult
from scripts.check_llm_endpoint import provider_kind, safe_display_base_url
from scripts.smoke_real_structured_agents import (
    ObservingStructuredClient,
    StructuredCallObservation,
)
from storage.models.base import Base
from storage.models.intervention import InterventionEventModel
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.user import UserModel

SQLITE_DIRECTORY = Path(tempfile.gettempdir()) / "psych-agent-smoke"


class RuntimeSmokeExitCode(IntEnum):
    """Stable outcomes for manual model-assisted runtime validation."""

    SUCCESS = 0
    CONFIGURATION = 2
    RUNTIME = 3
    DATABASE = 4
    ACCEPTANCE = 5
    CLEANUP = 6


@dataclass(frozen=True)
class RuntimeSmokeOptions:
    """Command options separated from argparse for deterministic tests."""

    ascii_output: bool = False
    json_output: bool = False
    keep_db: bool = False


@dataclass(frozen=True)
class AgentRuntimeStats:
    """Safe aggregate for one structured Agent across both turns."""

    agent: str
    calls: int
    api_success: int
    schema_success: int
    fallback_used: int
    effective_result_available: int
    latency_ms: list[int]


@dataclass
class RuntimeSmokeSummary:
    """Machine-readable acceptance result without prompts or secrets."""

    provider_kind: str
    base_url: str
    model: str
    database_driver: str = "sqlite+aiosqlite"
    run_id: str = ""
    user_id: str = ""
    session_id: str = ""
    turns_completed: int = 0
    users_rows: int = 0
    sessions_rows: int = 0
    messages_rows: int = 0
    state_versions_rows: int = 0
    intervention_events_rows: int = 0
    session_status: str | None = None
    source_message_ids_match: bool = False
    repository_boundary_valid: bool = False
    model_components_valid: bool = False
    agents: list[AgentRuntimeStats] = field(default_factory=list)
    structured_calls: int = 0
    api_attempts: int = 0
    fallback_count: int = 0
    database_kept: bool = False
    database_path: str | None = None
    success: bool = False
    error_type: str | None = None
    error_message_safe: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the manual runtime smoke command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--ascii", action="store_true", dest="ascii_output")
    output.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--keep-db", action="store_true")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> RuntimeSmokeOptions:
    """Parse command-line arguments into a stable options object."""

    args = build_parser().parse_args(argv)
    return RuntimeSmokeOptions(
        ascii_output=args.ascii_output,
        json_output=args.json_output,
        keep_db=args.keep_db,
    )


async def run_runtime_smoke(
    settings: Settings,
    options: RuntimeSmokeOptions,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_policy: RetryPolicy | None = None,
) -> tuple[RuntimeSmokeSummary, RuntimeSmokeExitCode]:
    """Run exactly two real structured-Agent turns on an isolated SQLite file."""

    run_id = uuid4().hex[:12]
    user_id = f"smoke-model-runtime-user-{run_id}"
    session_id = f"smoke-model-runtime-session-{run_id}"
    database_path = SQLITE_DIRECTORY / f"model_sqlalchemy_runtime_{run_id}.db"
    summary = RuntimeSmokeSummary(
        provider_kind=provider_kind(settings.llm_base_url),
        base_url=safe_display_base_url(settings.llm_base_url),
        model=settings.structured_model_name.strip() or "<missing>",
        run_id=run_id,
        user_id=user_id,
        session_id=session_id,
    )
    runtime: ApplicationRuntime | None = None
    observer: ObservingStructuredClient | None = None
    exit_code = RuntimeSmokeExitCode.SUCCESS

    def build_http_client(active_settings: Settings) -> OpenAICompatibleHTTPClient:
        return OpenAICompatibleHTTPClient.from_settings(
            active_settings,
            retry_policy=retry_policy,
            transport=transport,
        )

    def observe_client(
        formal_client: StructuredLLMClient,
        http_client: ManagedLLMClient,
    ) -> StructuredLLMClientProtocol:
        nonlocal observer
        observer = ObservingStructuredClient(formal_client, http_client)
        return observer

    try:
        SQLITE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        if database_path.exists():
            raise RuntimeConfigurationError("unique SQLite smoke path already exists")
        smoke_settings = settings.model_copy(
            update={
                "app_runtime_mode": "sqlalchemy_model",
                "database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}",
            }
        )
        runtime = await build_application_runtime(
            smoke_settings,
            http_client_factory=build_http_client,
            structured_client_wrapper=observe_client,
        )
        if runtime.engine is None or runtime.session_factory is None or observer is None:
            raise RuntimeConfigurationError("SQLAlchemy model runtime is incomplete")
        load_all_models()
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        results: list[ChatTurnResult] = []
        artificial_messages = (
            "I feel calm and want to organize one small task for tomorrow.",
            "That was useful; I want to choose one simple first step now.",
        )
        for message in artificial_messages:
            results.append(
                await runtime.orchestrator.handle_turn(
                    user_id=UserId(user_id),
                    session_id=SessionId(session_id),
                    text=message,
                )
            )
        summary.turns_completed = len(results)
        summary.repository_boundary_valid = all(
            isinstance(result, ChatTurnResult) for result in results
        )
        summary.model_components_valid = all(
            (
                isinstance(runtime.response_agent, ResponseAgent),
                isinstance(runtime.output_guard, OutputGuard),
                isinstance(runtime.feedback_evaluator, FeedbackEvaluator),
            )
        )
        await _populate_database_stats(summary, runtime)
        summary.agents = _aggregate_agent_stats(observer.observations)
        summary.structured_calls = len(observer.observations)
        assert runtime.http_client is not None
        summary.api_attempts = runtime.http_client.request_count
        summary.fallback_count = sum(item.fallback_used for item in summary.agents)
        summary.success = _acceptance_matches(summary)
        if not summary.success:
            exit_code = RuntimeSmokeExitCode.ACCEPTANCE
            summary.error_type = "RuntimeAcceptanceMismatch"
            summary.error_message_safe = "runtime smoke statistics did not match"
    except (LLMConfigurationError, RuntimeConfigurationError, ValidationError) as exc:
        exit_code = RuntimeSmokeExitCode.CONFIGURATION
        summary.error_type = type(exc).__name__
        summary.error_message_safe = "runtime configuration failed safely"
    except SQLAlchemyError as exc:
        exit_code = RuntimeSmokeExitCode.DATABASE
        summary.error_type = type(exc).__name__
        summary.error_message_safe = "temporary SQLite runtime failed safely"
    except Exception as exc:
        exit_code = RuntimeSmokeExitCode.RUNTIME
        summary.error_type = type(exc).__name__
        summary.error_message_safe = "model-assisted runtime failed safely"
    finally:
        if observer is not None and not summary.agents:
            summary.agents = _aggregate_agent_stats(observer.observations)
            summary.structured_calls = len(observer.observations)
            summary.fallback_count = sum(item.fallback_used for item in summary.agents)
        if runtime is not None:
            if runtime.http_client is not None:
                summary.api_attempts = runtime.http_client.request_count
            await runtime.aclose()
        if options.keep_db and database_path.exists():
            summary.database_kept = True
            summary.database_path = str(database_path)
        else:
            try:
                database_path.unlink(missing_ok=True)
            except OSError as exc:
                summary.success = False
                exit_code = RuntimeSmokeExitCode.CLEANUP
                summary.error_type = type(exc).__name__
                summary.error_message_safe = "temporary SQLite cleanup failed"
    return summary, exit_code


async def _populate_database_stats(
    summary: RuntimeSmokeSummary,
    runtime: ApplicationRuntime,
) -> None:
    """Read isolated counters and verify persisted state provenance."""

    session_factory = runtime.session_factory
    if session_factory is None:
        raise RuntimeConfigurationError("SQLAlchemy session factory is missing")
    async with session_factory() as session:
        summary.users_rows = int(
            await session.scalar(
                select(func.count())
                .select_from(UserModel)
                .where(UserModel.id == summary.user_id)
            )
            or 0
        )
        summary.sessions_rows = int(
            await session.scalar(
                select(func.count())
                .select_from(SessionModel)
                .where(
                    SessionModel.user_id == summary.user_id,
                    SessionModel.session_id == summary.session_id,
                )
            )
            or 0
        )
        session_row = await session.scalar(
            select(SessionModel).where(
                SessionModel.user_id == summary.user_id,
                SessionModel.session_id == summary.session_id,
            )
        )
        if session_row is None:
            raise RuntimeConfigurationError("runtime session row is missing")
        messages = list(
            (
                await session.scalars(
                    select(MessageModel)
                    .where(MessageModel.session_pk == session_row.id)
                    .order_by(MessageModel.sequence_number)
                )
            ).all()
        )
        state_rows = list(
            (
                await session.scalars(
                    select(SessionStateVersionModel)
                    .where(SessionStateVersionModel.session_pk == session_row.id)
                    .order_by(SessionStateVersionModel.version)
                )
            ).all()
        )
        summary.messages_rows = len(messages)
        summary.state_versions_rows = len(state_rows)
        summary.intervention_events_rows = int(
            await session.scalar(
                select(func.count())
                .select_from(InterventionEventModel)
                .where(InterventionEventModel.session_pk == session_row.id)
            )
            or 0
        )
        summary.session_status = session_row.status
        user_message_ids = [row.id for row in messages if row.role == "user"]
        summary.source_message_ids_match = [
            row.source_message_id for row in state_rows
        ] == user_message_ids


def _aggregate_agent_stats(
    observations: list[StructuredCallObservation],
) -> list[AgentRuntimeStats]:
    """Aggregate safe per-call observations in stable Agent order."""

    result: list[AgentRuntimeStats] = []
    for agent_name in (
        "feedback_evaluator",
        "risk_agent",
        "state_tracker",
        "strategy_planner",
        "response_agent",
        "output_guard",
    ):
        agent_observations = [
            item for item in observations if item.agent == agent_name
        ]
        schema_success = sum(item.schema_success for item in agent_observations)
        result.append(
            AgentRuntimeStats(
                agent=agent_name,
                calls=len(agent_observations),
                api_success=sum(item.api_success for item in agent_observations),
                schema_success=schema_success,
                fallback_used=len(agent_observations) - schema_success,
                effective_result_available=len(agent_observations),
                latency_ms=[item.latency_ms for item in agent_observations],
            )
        )
    return result


def _acceptance_matches(summary: RuntimeSmokeSummary) -> bool:
    """Require successful real structured calls per Agent and DB contract."""

    expected_calls = {
        "feedback_evaluator": 1,
        "risk_agent": 2,
        "state_tracker": 2,
        "strategy_planner": 2,
        "response_agent": 2,
        "output_guard": 2,
    }

    return all(
        (
            summary.turns_completed == 2,
            summary.users_rows == 1,
            summary.sessions_rows == 1,
            summary.messages_rows == 4,
            summary.state_versions_rows == 2,
            summary.intervention_events_rows == 2,
            summary.session_status == "active",
            summary.source_message_ids_match,
            summary.repository_boundary_valid,
            summary.model_components_valid,
            summary.structured_calls == sum(expected_calls.values()) + 1,
            summary.fallback_count == 0,
            all(
                item.calls == expected_calls[item.agent]
                and item.api_success == expected_calls[item.agent]
                and item.schema_success == expected_calls[item.agent]
                and item.effective_result_available == expected_calls[item.agent]
                for item in summary.agents
            ),
        )
    )


def render_summary(summary: RuntimeSmokeSummary, *, json_output: bool) -> str:
    """Render safe statistics without requests, responses, or credentials."""

    payload = asdict(summary)
    if json_output:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)
    lines = [
        f"{key}={value}"
        for key, value in payload.items()
        if key != "agents"
    ]
    lines.extend(
        " ".join(f"{key}={value}" for key, value in asdict(agent).items())
        for agent in summary.agents
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the manual smoke using shared Settings and safe output."""

    options = parse_options(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        summary = RuntimeSmokeSummary(
            provider_kind="unknown",
            base_url="<invalid-settings>",
            model="<invalid-settings>",
            error_type=type(exc).__name__,
            error_message_safe="settings validation failed safely",
        )
        print(render_summary(summary, json_output=options.json_output))
        return int(RuntimeSmokeExitCode.CONFIGURATION)
    summary, exit_code = asyncio.run(run_runtime_smoke(settings, options))
    print(render_summary(summary, json_output=options.json_output))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
