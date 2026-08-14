"""Final HTTP acceptance tests for the persisted adaptive D loop."""

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.routers.sessions import SessionCloseResponse
from app.config import Settings
from app.main import create_app
from runtime.application import ApplicationRuntime
from runtime.factory import build_application_runtime
from schemas.feedback import FeedbackLabel, ObjectiveProgress, StrategyFit
from schemas.intervention import InterventionStatus
from schemas.memory import MemorySensitivity, MemoryType
from schemas.messages import ChatTurnResult, MessageRole
from schemas.state import SessionState
from schemas.summary import RollingSummary
from storage.models.base import Base
from storage.models.intervention import InterventionEventModel
from storage.models.memory import (
    MEMORY_STATUS_ACTIVE,
    LongTermMemoryModel,
)
from storage.models.message import MessageModel
from storage.models.registry import load_all_models
from storage.models.session import SESSION_STATUS_CLOSED, SessionModel
from storage.models.session_state import SessionStateVersionModel
from storage.models.summary import RollingSummaryVersionModel
from storage.models.user import UserModel

TURNS = (
    "最近工作压力很大，我感觉自己有些卡住了。",
    "这个回应有帮助，但请一次只给我一个步骤，不要一次给太多建议。",
    "那先告诉我今天可以怎么做，只给我第一步。",
)


@dataclass(frozen=True)
class AcceptanceSnapshot:
    """Typed ORM rows read after the public API workflow has completed."""

    user: UserModel
    session: SessionModel
    messages: list[MessageModel]
    states: list[SessionStateVersionModel]
    interventions: list[InterventionEventModel]
    summaries: list[RollingSummaryVersionModel]
    memories: list[LongTermMemoryModel]


@dataclass(frozen=True)
class AcceptanceResult:
    """Public responses and persisted result for one isolated scenario."""

    turns: list[ChatTurnResult]
    close: SessionCloseResponse
    snapshot: AcceptanceSnapshot


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _settings(database_url: str) -> Settings:
    return Settings(
        app_env="testing",
        app_runtime_mode="sqlalchemy_fake",
        database_url=database_url,
        llm_api_key="d-api-acceptance-key",
        main_model_name="d-api-acceptance-model",
        structured_model_name="d-api-acceptance-model",
        safety_model_name="d-api-acceptance-model",
        _env_file=None,
    )


async def _runtime_builder(settings: Settings) -> ApplicationRuntime:
    return await build_application_runtime(settings)


async def _create_schema(database_url: str) -> None:
    load_all_models()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _enable_memory(database_url: str, user_id: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                update(UserModel)
                .where(UserModel.id == user_id)
                .values(memory_enabled=True)
            )
            assert result.rowcount == 1, "the API-created user must exist before consent"
    finally:
        await engine.dispose()


async def _load_snapshot(
    database_url: str,
    *,
    user_id: str,
    session_id: str,
) -> AcceptanceSnapshot:
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as connection:
            user = (
                await connection.execute(
                    select(UserModel).where(UserModel.id == user_id)
                )
            ).scalar_one()
            session = (
                await connection.execute(
                    select(SessionModel).where(SessionModel.id == session_id)
                )
            ).scalar_one()
            messages = list(
                (
                    await connection.execute(
                        select(MessageModel)
                        .where(MessageModel.session_id == session_id)
                        .order_by(MessageModel.sequence_number)
                    )
                ).scalars()
            )
            states = list(
                (
                    await connection.execute(
                        select(SessionStateVersionModel)
                        .where(SessionStateVersionModel.session_id == session_id)
                        .order_by(SessionStateVersionModel.version)
                    )
                ).scalars()
            )
            interventions = list(
                (
                    await connection.execute(
                        select(InterventionEventModel)
                        .join(
                            MessageModel,
                            InterventionEventModel.assistant_message_id == MessageModel.id,
                        )
                        .where(InterventionEventModel.session_id == session_id)
                        .order_by(
                            MessageModel.sequence_number,
                            InterventionEventModel.id,
                        )
                    )
                ).scalars()
            )
            summaries = list(
                (
                    await connection.execute(
                        select(RollingSummaryVersionModel)
                        .where(RollingSummaryVersionModel.session_id == session_id)
                        .order_by(RollingSummaryVersionModel.summary_version)
                    )
                ).scalars()
            )
            memories = list(
                (
                    await connection.execute(
                        select(LongTermMemoryModel)
                        .where(LongTermMemoryModel.user_id == user_id)
                        .order_by(LongTermMemoryModel.created_at)
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()
    return AcceptanceSnapshot(
        user=user,
        session=session,
        messages=messages,
        states=states,
        interventions=interventions,
        summaries=summaries,
        memories=memories,
    )


async def _run_api_scenario(
    tmp_path: Path,
    *,
    suffix: str,
    memory_enabled: bool,
) -> AcceptanceResult:
    database_url = _database_url(tmp_path / f"d-api-acceptance-{suffix}.db")
    user_id = f"d-api-user-{suffix}"
    session_id = f"d-api-session-{suffix}"
    await _create_schema(database_url)
    app = create_app(
        runtime_builder=_runtime_builder,
        settings_provider=lambda: _settings(database_url),
    )

    turn_results: list[ChatTurnResult] = []
    with TestClient(app) as client:
        for text in TURNS:
            response = client.post(
                "/chat/turn",
                json={
                    "user_id": user_id,
                    "session_id": session_id,
                    "message": text,
                },
            )
            assert response.status_code == 200, response.text
            result = ChatTurnResult.model_validate(response.json())
            assert result.status == "ok"
            turn_results.append(result)

        if memory_enabled:
            await _enable_memory(database_url, user_id)

        close_response = client.post(
            "/sessions/close",
            json={
                "user_id": user_id,
                "session_id": session_id,
                "reason": "d-final-acceptance",
            },
        )
        assert close_response.status_code == 200, close_response.text
        close_result = SessionCloseResponse.model_validate(close_response.json())

    snapshot = await _load_snapshot(
        database_url,
        user_id=user_id,
        session_id=session_id,
    )
    return AcceptanceResult(turn_results, close_result, snapshot)


def _assert_turns_and_messages(result: AcceptanceResult) -> None:
    assert [turn.state_version for turn in result.turns] == [1, 2, 3]
    assistant_ids = [str(turn.message_id) for turn in result.turns]
    assert all(assistant_ids)
    assert len(set(assistant_ids)) == 3

    messages = result.snapshot.messages
    assert len(messages) == 6
    assert [message.sequence_number for message in messages] == [1, 2, 3, 4, 5, 6]
    assert [message.role for message in messages] == [
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
    ]
    assert [message.content for message in messages if message.role == MessageRole.USER] == list(
        TURNS
    )
    assert [message.id for message in messages if message.role == MessageRole.ASSISTANT] == (
        assistant_ids
    )
    assert len({message.sequence_number for message in messages}) == 6
    assert {message.session_id for message in messages} == {result.snapshot.session.id}


def _assert_states(result: AcceptanceResult) -> None:
    states = result.snapshot.states
    user_message_ids = [
        message.id
        for message in result.snapshot.messages
        if message.role == MessageRole.USER
    ]
    assert len(states) == 3
    assert [row.version for row in states] == [1, 2, 3]
    assert [row.source_message_id for row in states] == user_message_ids

    latest = SessionState.model_validate(states[-1].state_json)
    assert latest.version == 3
    concrete_preferences = [
        preference
        for preference in latest.user_preferences
        if "具体" in preference.value and "行动建议" in preference.value
    ]
    assert concrete_preferences
    assert all(
        str(preference.source.message_id) in {user_message_ids[1], user_message_ids[2]}
        for preference in concrete_preferences
    )
    assert any(
        preference.value == "strategy_adjustment:adjust_format_or_support_style"
        and str(preference.source.message_id) == user_message_ids[1]
        for preference in latest.user_preferences
    )


def _assert_interventions(result: AcceptanceResult) -> None:
    interventions = result.snapshot.interventions
    assistant_ids = [str(turn.message_id) for turn in result.turns]
    assert len(interventions) == 3
    assert [row.status for row in interventions] == [
        InterventionStatus.EVALUATED.value,
        InterventionStatus.EVALUATED.value,
        InterventionStatus.PENDING.value,
    ]
    assert [row.assistant_message_id for row in interventions] == assistant_ids
    assert len({row.assistant_message_id for row in interventions}) == 3

    first, second, third = interventions
    assert first.explicit_feedback == FeedbackLabel.MIXED.value
    assert first.strategy_fit == StrategyFit.MIXED.value
    assert first.objective_progress == ObjectiveProgress.PARTIAL.value
    assert first.recommended_adjustment == "adjust_format_or_support_style"
    assert first.feedback_confidence is not None
    assert first.evaluated_at is not None

    assert second.explicit_feedback == FeedbackLabel.ABSENT.value
    assert second.strategy_fit == StrategyFit.UNKNOWN.value
    assert second.objective_progress == ObjectiveProgress.UNKNOWN.value
    assert second.recommended_adjustment is None
    assert second.feedback_confidence is not None
    assert second.evaluated_at is not None

    assert third.explicit_feedback is None
    assert third.strategy_fit is None
    assert third.objective_progress is None
    assert third.recommended_adjustment is None
    assert third.feedback_confidence is None
    assert third.evaluated_at is None


def _assert_summaries(result: AcceptanceResult) -> None:
    summaries = result.snapshot.summaries
    messages_by_id = {message.id: message for message in result.snapshot.messages}
    assert summaries
    versions = [row.summary_version for row in summaries]
    assert versions == list(range(1, len(summaries) + 1))
    assert len(versions) == len(set(versions))
    assert result.snapshot.session.current_summary_version == versions[-1]

    previous_to_sequence = 0
    for row in summaries:
        summary = RollingSummary.model_validate(row.summary_json)
        assert str(summary.session_id) == result.snapshot.session.id
        assert row.covered_from_message_id in messages_by_id
        assert row.covered_to_message_id in messages_by_id
        from_sequence = messages_by_id[row.covered_from_message_id].sequence_number
        to_sequence = messages_by_id[row.covered_to_message_id].sequence_number
        assert from_sequence <= to_sequence
        assert to_sequence >= previous_to_sequence
        previous_to_sequence = to_sequence

    latest = RollingSummary.model_validate(summaries[-1].summary_json)
    assert str(latest.covered_to) == result.snapshot.messages[-1].id
    source_ids = [str(message_id) for message_id in latest.source_message_ids]
    assert source_ids
    assert len(source_ids) == len(set(source_ids))
    assert set(source_ids) <= set(messages_by_id)
    assert latest.important_user_statements or latest.current_problem


def _assert_closed_session(result: AcceptanceResult, *, memory_enabled: bool) -> None:
    session = result.snapshot.session
    assert session.status == SESSION_STATUS_CLOSED
    assert session.ended_at is not None
    assert session.current_state_version == 3
    assert session.current_summary_version == result.snapshot.summaries[-1].summary_version
    assert session.next_message_sequence == 7
    assert session.user_id == result.snapshot.user.id
    assert result.snapshot.user.memory_enabled is memory_enabled


async def test_authorized_multiturn_api_persists_complete_d_loop(
    tmp_path: Path,
) -> None:
    result = await _run_api_scenario(
        tmp_path,
        suffix="authorized",
        memory_enabled=True,
    )
    _assert_turns_and_messages(result)
    _assert_states(result)
    _assert_interventions(result)
    _assert_summaries(result)
    _assert_closed_session(result, memory_enabled=True)

    assert result.close.model_dump(mode="json") == {
        "session_id": "d-api-session-authorized",
        "candidate_memory_count": 1,
        "memory_write_count": 1,
        "status": "closed",
    }
    assert len(result.snapshot.memories) == 1
    memory = result.snapshot.memories[0]
    user_message_ids = {
        message.id
        for message in result.snapshot.messages
        if message.role == MessageRole.USER
    }
    assert memory.memory_type == MemoryType.INTERACTION_PREFERENCE.value
    assert memory.status == MEMORY_STATUS_ACTIVE
    assert "小步骤" in memory.content
    assert memory.sensitivity == MemorySensitivity.LOW.value
    assert 0.0 <= memory.confidence <= 1.0
    assert memory.source_message_ids
    assert len(memory.source_message_ids) == len(set(memory.source_message_ids))
    assert set(memory.source_message_ids) <= user_message_ids
    assert result.snapshot.messages[2].id in memory.source_message_ids
    assert memory.reinforcement_count == 0


async def test_unconsented_multiturn_api_closes_without_memory_write(
    tmp_path: Path,
) -> None:
    result = await _run_api_scenario(
        tmp_path,
        suffix="unconsented",
        memory_enabled=False,
    )
    _assert_turns_and_messages(result)
    _assert_states(result)
    _assert_interventions(result)
    _assert_summaries(result)
    _assert_closed_session(result, memory_enabled=False)

    assert result.close.model_dump(mode="json") == {
        "session_id": "d-api-session-unconsented",
        "candidate_memory_count": 1,
        "memory_write_count": 0,
        "status": "closed",
    }
    assert result.snapshot.memories == []
