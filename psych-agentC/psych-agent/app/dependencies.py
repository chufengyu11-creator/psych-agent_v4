"""FastAPI dependency factories for the initial in-memory application."""

from __future__ import annotations

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository

_orchestrator: TurnOrchestrator | None = None


def build_in_memory_orchestrator() -> TurnOrchestrator:
    """Construct a full fake pipeline with in-memory repositories."""

    return TurnOrchestrator(
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
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=InMemoryInterventionRepository(),
        task_queue=NoopTaskQueue(),
    )


def get_orchestrator() -> TurnOrchestrator:
    """Return the process-local orchestrator used by API routes."""

    global _orchestrator
    if _orchestrator is None:
        _orchestrator = build_in_memory_orchestrator()
    return _orchestrator
