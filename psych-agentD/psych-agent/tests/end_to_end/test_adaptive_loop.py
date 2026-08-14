"""End-to-end tests for the in-memory adaptive feedback loop."""

from __future__ import annotations

import pytest

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import SessionId, UserId
from schemas.intervention import InterventionStatus
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository


def _build_adaptive_loop() -> tuple[
    TurnOrchestrator,
    InMemoryInterventionRepository,
    InMemoryStateRepository,
]:
    """Build the real fake-agent loop with public in-memory repositories."""

    interventions = InMemoryInterventionRepository()
    states = InMemoryStateRepository()
    orchestrator = TurnOrchestrator(
        risk_agent=FakeRiskAgent(),
        state_tracker=FakeStateTracker(),
        feedback_evaluator=FakeFeedbackEvaluator(),
        strategy_planner=FakeStrategyPlanner(),
        response_agent=FakeResponseAgent(),
        output_guard=FakeOutputGuard(),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=MemoryRetriever(),
        message_repository=InMemoryMessageRepository(),
        state_repository=states,
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=interventions,
        task_queue=NoopTaskQueue(),
    )
    return orchestrator, interventions, states


@pytest.mark.asyncio
async def test_rejection_consumes_pending_intervention_and_switches_strategy() -> None:
    """A rejected reflective intervention should be evaluated and replaced."""

    orchestrator, interventions, _ = _build_adaptive_loop()
    session_id = SessionId("session_adaptive_rejection")
    user_id = UserId("user_adaptive_rejection")

    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近和直属领导沟通很紧张，每次开会前都会焦虑。",
    )

    first_pending = await interventions.get_pending(session_id)
    assert first_pending is not None
    assert first_pending.status == InterventionStatus.PENDING
    assert first_pending.strategy == "reflective_listening"
    first_intervention_id = first_pending.intervention_id
    first_assistant_message_id = first_pending.assistant_message_id
    first_strategy = first_pending.strategy

    second_turn = await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我不想继续分析情绪，我现在更想知道下一步具体怎么做。",
    )

    records = await interventions.list_for_session(session_id)
    assert len(records) == 2
    evaluated_first, pending_second = records

    assert evaluated_first.intervention_id == first_intervention_id
    assert evaluated_first.assistant_message_id == first_assistant_message_id
    assert evaluated_first.strategy == first_strategy
    assert evaluated_first.status == InterventionStatus.EVALUATED
    assert evaluated_first.explicit_feedback == "negative"
    assert evaluated_first.strategy_fit == "poor"
    assert evaluated_first.objective_progress == "not_achieved"
    assert evaluated_first.recommended_adjustment == "switch_or_clarify_strategy"
    assert evaluated_first.observed_response

    assert pending_second.intervention_id != first_intervention_id
    assert pending_second.assistant_message_id != first_assistant_message_id
    assert pending_second.status == InterventionStatus.PENDING
    assert pending_second.strategy != "reflective_listening"
    assert pending_second.strategy == "clarification"
    assert await interventions.get_pending(session_id) == pending_second

    assert second_turn.state_version == 2
    assert second_turn.message_id == pending_second.assistant_message_id
    assert "校准" in second_turn.response


@pytest.mark.asyncio
async def test_continuation_is_evaluated_as_absent_not_positive() -> None:
    """Continuing the story consumes pending feedback without inventing acceptance."""

    orchestrator, interventions, _ = _build_adaptive_loop()
    session_id = SessionId("session_adaptive_absent")
    user_id = UserId("user_adaptive_absent")

    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近和直属领导沟通很紧张，每次开会前都会焦虑。",
    )
    first_pending = await interventions.get_pending(session_id)
    assert first_pending is not None

    second_turn = await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="今天开会的时候，我还是很紧张。",
    )

    records = await interventions.list_for_session(session_id)
    assert len(records) == 2
    evaluated_first, pending_second = records

    assert evaluated_first.intervention_id == first_pending.intervention_id
    assert evaluated_first.status == InterventionStatus.EVALUATED
    assert evaluated_first.explicit_feedback == "absent"
    assert evaluated_first.strategy_fit == "unknown"
    assert evaluated_first.objective_progress == "unknown"
    assert evaluated_first.explicit_feedback != "positive"
    assert evaluated_first.observed_response

    assert pending_second.intervention_id != first_pending.intervention_id
    assert pending_second.status == InterventionStatus.PENDING
    assert await interventions.get_pending(session_id) == pending_second
    assert second_turn.state_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    [
        "另外我还想问一下最近睡不好的问题。",
        "接下来可以给我一个具体步骤吗？",
    ],
)
async def test_topic_switch_or_step_request_without_evaluation_is_absent(
    user_text: str,
) -> None:
    """A new topic or bare format request must not invent strategy feedback."""

    orchestrator, interventions, _ = _build_adaptive_loop()
    session_id = SessionId(f"session_adaptive_absent_{len(user_text)}")
    user_id = UserId(f"user_adaptive_absent_{len(user_text)}")

    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近和直属领导沟通很紧张，每次开会前都会焦虑。",
    )
    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text=user_text,
    )

    evaluated_first = (await interventions.list_for_session(session_id))[0]
    assert evaluated_first.status == InterventionStatus.EVALUATED
    assert evaluated_first.explicit_feedback == "absent"
    assert evaluated_first.objective_progress == "unknown"
    assert evaluated_first.strategy_fit == "unknown"
    assert evaluated_first.explicit_feedback not in {"negative", "positive"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    [
        "I am okay, but work is still stressful.",
        "My manager was helpful today.",
        "这个同事很有帮助，我今天还是很焦虑。",
        "我可以，但是今天还很紧张。",
    ],
)
async def test_ambiguous_feedback_does_not_create_strategy_adjustment(
    user_text: str,
) -> None:
    orchestrator, interventions, states = _build_adaptive_loop()
    session_id = SessionId(f"session_ambiguous_{len(user_text)}")
    user_id = UserId(f"user_ambiguous_{len(user_text)}")

    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近与领导沟通很紧张。",
    )
    second_turn = await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text=user_text,
    )

    records = await interventions.list_for_session(session_id)
    evaluated_first, pending_second = records
    assert evaluated_first.explicit_feedback == "absent"
    assert evaluated_first.objective_progress == "unknown"
    assert evaluated_first.strategy_fit == "unknown"
    assert evaluated_first.recommended_adjustment is None
    assert evaluated_first.explicit_feedback not in {"positive", "negative", "mixed"}
    state = await states.get_current(session_id)
    assert state is not None
    assert all(
        not preference.value.startswith("strategy_adjustment:")
        for preference in state.user_preferences
    )
    assert pending_second.status == InterventionStatus.PENDING
    assert second_turn.message_id == pending_second.assistant_message_id


@pytest.mark.asyncio
async def test_explicit_mixed_feedback_uses_stable_adjustment() -> None:
    """Partial acceptance plus a format request uses the fixed mixed control code."""

    orchestrator, interventions, states = _build_adaptive_loop()
    session_id = SessionId("session_explicit_mixed")
    user_id = UserId("user_explicit_mixed")

    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近与领导沟通很紧张。",
    )
    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="这个回应有帮助，不过不要一次给我太多建议。",
    )

    records = await interventions.list_for_session(session_id)
    evaluated_first, pending_second = records
    assert evaluated_first.explicit_feedback == "mixed"
    assert evaluated_first.objective_progress == "partial"
    assert evaluated_first.strategy_fit == "mixed"
    assert evaluated_first.recommended_adjustment == "adjust_format_or_support_style"
    state = await states.get_current(session_id)
    assert state is not None
    assert any(
        preference.value
        == "strategy_adjustment:adjust_format_or_support_style"
        for preference in state.user_preferences
    )
    assert pending_second.status == InterventionStatus.PENDING
