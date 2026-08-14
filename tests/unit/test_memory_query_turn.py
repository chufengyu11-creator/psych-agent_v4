"""Turn-level tests for the deterministic memory-query path."""

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import MemoryId, MessageId, SessionId, SourceReference, UserId
from schemas.context import ResponseContext
from schemas.memory import (
    LongTermMemory,
    MemorySensitivity,
    MemoryType,
    RetrievedMemories,
)
from schemas.safety import DraftResponse
from schemas.state import ConversationPhase, SessionState
from schemas.strategy import StrategyPlan, StrategyPlannerInput, StrategyType
from services.context_builder import ContextBuilder
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository


class RecordingStrategyPlanner:
    def __init__(self) -> None:
        self.calls: list[StrategyPlannerInput] = []

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        self.calls.append(payload)
        return StrategyPlan(
            conversation_phase=ConversationPhase.EXPLORATION,
            primary_strategy=StrategyType.SUMMARIZATION,
            objective="answer the memory question from retrieved context",
            reason="unified strategy path fixture",
        )


class RecordingResponseAgent:
    def __init__(self) -> None:
        self.calls: list[ResponseContext] = []

    async def generate(self, context: ResponseContext) -> DraftResponse:
        self.calls.append(context)
        return DraftResponse(
            text="我记得你提过音乐节门票；另外还有 1 条待确认记录。",
            referenced_memory_ids=["mem_turn_festival"],
        )


class StaticMemoryRetriever:
    def __init__(self, memories: RetrievedMemories) -> None:
        self.memories = memories
        self.calls: list[tuple[str, str, int]] = []

    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        self.calls.append((user_id, query, limit))
        return self.memories


def _festival_memory() -> LongTermMemory:
    return LongTermMemory(
        id=MemoryId("mem_turn_festival"),
        memory_type=MemoryType.EPISODIC,
        content="用户说自己抢到了音乐节门票。",
        sensitivity=MemorySensitivity.LOW,
        confidence=0.95,
        source=[SourceReference(message_id=MessageId("msg_old_festival"))],
    )


async def test_memory_query_reads_once_and_uses_unified_model_path() -> None:
    user_id = UserId("memory-query-turn-user")
    session_id = SessionId("memory-query-turn-session")
    retriever = StaticMemoryRetriever(
        RetrievedMemories(
            active_memories=[_festival_memory()],
            pending_confirmation_count=1,
        )
    )
    planner = RecordingStrategyPlanner()
    responder = RecordingResponseAgent()
    orchestrator = TurnOrchestrator(
        risk_agent=FakeRiskAgent(),
        state_tracker=FakeStateTracker(),
        feedback_evaluator=FakeFeedbackEvaluator(),
        strategy_planner=planner,
        response_agent=responder,
        output_guard=FakeOutputGuard(),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=retriever,
        message_repository=InMemoryMessageRepository(),
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        task_queue=NoopTaskQueue(),
    )

    result = await orchestrator.handle_turn(
        user_id,
        session_id,
        "你还记得我吗？",
    )

    assert result.status == "ok"
    assert result.state_version == 1
    assert "音乐节门票" in result.response
    assert "1 条待确认" in result.response
    assert retriever.calls == [
        (str(user_id), "你还记得我吗？", 8),
    ]
    assert len(planner.calls) == 1
    assert planner.calls[0].current_user_message == "你还记得我吗？"
    assert planner.calls[0].memory_query_intent.value == "existence"
    assert planner.calls[0].memories is retriever.memories
    assert len(responder.calls) == 1
    assert responder.calls[0].memory_query_intent.value == "existence"
    assert responder.calls[0].memories is retriever.memories
