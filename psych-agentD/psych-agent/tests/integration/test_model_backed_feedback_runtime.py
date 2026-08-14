"""Runtime integration tests for the model-backed feedback evaluator."""

from collections.abc import Mapping

import pytest

from agents.feedback_evaluator import FeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from llm.structured_output import ModelT
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import SessionId, UserId
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository


class RecordingStructuredClient:
    """Return one scripted private draft or raise a scripted error."""

    def __init__(
        self,
        draft: dict[str, object] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.draft = draft or {}
        self.error = error
        self.calls: list[tuple[str, str, str | None, Mapping[str, object] | None]] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        self.calls.append((prompt, output_model.__name__, model_name, metadata))
        if self.error is not None:
            raise self.error
        return output_model.model_validate(self.draft)


def _draft(
    label: str,
    progress: str,
    fit: str,
    quote: str | None,
    *,
    evidence_id: str | None,
) -> dict[str, object]:
    return {
        "explicit_feedback": label,
        "objective_progress": progress,
        "strategy_fit": fit,
        "confidence": 0.9,
        "evidence_message_id": evidence_id,
        "evidence_quote": quote,
    }


def _runtime(
    client: RecordingStructuredClient,
) -> tuple[TurnOrchestrator, InMemoryInterventionRepository]:
    interventions = InMemoryInterventionRepository()
    orchestrator = TurnOrchestrator(
        risk_agent=FakeRiskAgent(),
        state_tracker=FakeStateTracker(),
        feedback_evaluator=FeedbackEvaluator(client, model_name="feedback-runtime-model"),
        strategy_planner=FakeStrategyPlanner(),
        response_agent=FakeResponseAgent(),
        output_guard=FakeOutputGuard(),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=MemoryRetriever(),
        message_repository=InMemoryMessageRepository(),
        state_repository=InMemoryStateRepository(),
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=interventions,
        task_queue=NoopTaskQueue(),
    )
    return orchestrator, interventions


async def _run_two_turns(
    client: RecordingStructuredClient,
    second_text: str,
) -> InMemoryInterventionRepository:
    orchestrator, interventions = _runtime(client)
    user_id = UserId("feedback_runtime_user")
    session_id = SessionId("feedback_runtime_session")
    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text="我最近与领导沟通很紧张。",
    )
    assert client.calls == []
    await orchestrator.handle_turn(
        user_id=user_id,
        session_id=session_id,
        text=second_text,
    )
    return interventions


@pytest.mark.asyncio
async def test_model_feedback_success_enters_adaptive_loop() -> None:
    client = RecordingStructuredClient(
        _draft("positive", "achieved", "good", "普通", evidence_id="msg_3")
    )
    interventions = await _run_two_turns(client, "普通继续描述。")

    assert len(client.calls) == 1
    prompt, output_name, model_name, metadata = client.calls[0]
    assert "INPUT_JSON" in prompt
    assert output_name == "_GroundedFeedbackDraft"
    assert model_name == "feedback-runtime-model"
    assert metadata == {"agent": "feedback_evaluator"}
    completed = (await interventions.list_for_session(SessionId("feedback_runtime_session")))[0]
    assert completed.explicit_feedback == "positive"
    assert completed.strategy_fit == "good"
    assert completed.objective_progress == "achieved"


@pytest.mark.asyncio
async def test_model_exception_uses_one_mixed_feedback_fallback() -> None:
    client = RecordingStructuredClient(error=RuntimeError("model unavailable"))
    interventions = await _run_two_turns(client, "这个回应有帮助，但请一次只给我一个步骤。")

    records = await interventions.list_for_session(SessionId("feedback_runtime_session"))
    assert len(client.calls) == 1
    assert len(records) == 2
    assert records[0].explicit_feedback == "mixed"
    assert records[0].recommended_adjustment == "adjust_format_or_support_style"


@pytest.mark.asyncio
async def test_invalid_model_evidence_uses_grounded_fallback() -> None:
    client = RecordingStructuredClient(
        _draft("positive", "achieved", "good", "没帮助", evidence_id="wrong_id")
    )
    interventions = await _run_two_turns(client, "这对我没帮助。")

    records = await interventions.list_for_session(SessionId("feedback_runtime_session"))
    assert len(client.calls) == 1
    assert records[0].explicit_feedback == "negative"
    assert records[0].strategy_fit == "poor"
    assert records[0].recommended_adjustment == "switch_or_clarify_strategy"
