"""Tests for consuming completed deep state without recomputing it in N+1."""

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import MessageId, SessionId, UserId, utc_now
from schemas.deep_state import DeepStateCandidate
from schemas.state import (
    GoalUpdate,
    StateDelta,
    StateOperation,
    StateTrackerInput,
    StrategyPreference,
)
from schemas.strategy import StrategyPlan, StrategyPlannerInput
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.models.deep_state import DEEP_STATE_STATUS_APPLIED
from storage.repositories.deep_state_repository import InMemoryDeepStateRepository
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository


class ExplicitFastStateTracker:
    """Emit one explicit current goal that must beat older deep inference."""

    async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
        return StateDelta(
            explicit_user_request=payload.current_message.content,
            current_turn_goal="current explicit goal",
            goal_updates=[
                GoalUpdate(
                    operation=StateOperation.UPDATE,
                    goal="current explicit goal",
                    source_message_id=payload.current_message.id,
                )
            ],
        )


class CapturingStrategyPlanner:
    """Capture the exact merged state supplied to strategy planning."""

    def __init__(self) -> None:
        self.inputs: list[StrategyPlannerInput] = []
        self._delegate = FakeStrategyPlanner()

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        self.inputs.append(payload.model_copy(deep=True))
        return await self._delegate.plan(payload)


async def test_latest_ready_deep_is_applied_before_current_fast_state() -> None:
    user_id = UserId("deep-n-plus-one-user")
    session_id = SessionId("deep-n-plus-one-session")
    messages = InMemoryMessageRepository()
    states = InMemoryStateRepository()
    deep_states = InMemoryDeepStateRepository()
    strategy = CapturingStrategyPlanner()

    source = await messages.create_user_message(
        user_id,
        session_id,
        "previous user message",
    )
    await messages.create_assistant_message(
        user_id,
        session_id,
        "previous assistant response",
    )
    deep_states.add_ready(
        DeepStateCandidate(
            id="deep_candidate_1",
            user_id=str(user_id),
            session_id=session_id,
            source_message_id=MessageId(source.id),
            source_message_sequence=source.sequence_number,
            base_state_version=0,
            pipeline_version="v1",
            delta=StateDelta(
                goal_updates=[
                    GoalUpdate(
                        operation=StateOperation.UPDATE,
                        goal="older inferred goal",
                        source_message_id=source.id,
                    )
                ],
                strategy_preferences=[
                    StrategyPreference(
                        operation=StateOperation.ADD,
                        value="deep inferred preference",
                        source_message_id=source.id,
                    )
                ],
            ),
            finished_at=utc_now(),
        )
    )
    orchestrator = TurnOrchestrator(
        risk_agent=FakeRiskAgent(),
        state_tracker=FakeStateTracker(),
        fast_state_tracker=ExplicitFastStateTracker(),
        feedback_evaluator=FakeFeedbackEvaluator(),
        strategy_planner=strategy,
        response_agent=FakeResponseAgent(),
        output_guard=FakeOutputGuard(),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=MemoryRetriever(),
        message_repository=messages,
        state_repository=states,
        deep_state_repository=deep_states,
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        task_queue=NoopTaskQueue(),
    )

    result = await orchestrator.handle_turn(
        user_id,
        session_id,
        "new explicit request",
    )

    assert result.state_version == 1
    assert len(strategy.inputs) == 1
    planned_state = strategy.inputs[0].session_state
    assert planned_state.session_goal == "current explicit goal"
    assert planned_state.current_turn_goal == "current explicit goal"
    assert [
        item.value for item in planned_state.user_preferences if item.active
    ] == ["deep inferred preference"]
    assert deep_states.status_of("deep_candidate_1") == DEEP_STATE_STATUS_APPLIED

    stored_state = await states.get_current(user_id, session_id)
    assert stored_state.version == 1
    assert stored_state.session_goal == "current explicit goal"
    assert [item.value for item in stored_state.user_preferences if item.active] == [
        "deep inferred preference"
    ]
