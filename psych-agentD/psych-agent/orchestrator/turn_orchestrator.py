"""Single-turn orchestration for the psychological support agent."""

from __future__ import annotations

from typing import Protocol

from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from orchestrator.post_turn_pipeline import TaskQueue
from orchestrator.safety_router import SafetyRouter
from schemas.common import SessionId, UserId
from schemas.feedback import FeedbackInput, FeedbackResult
from schemas.messages import ChatTurnResult, Message
from schemas.risk import RiskInput, RiskResult, RiskRoute
from schemas.safety import GuardInput
from schemas.state import StateDelta, StateTrackerInput
from schemas.strategy import StrategyPlan, StrategyPlannerInput
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InterventionRepository
from storage.repositories.message_repository import MessageRepository
from storage.repositories.state_repository import StateRepository
from storage.repositories.summary_repository import SummaryRepository


class RiskAnalyzer(Protocol):
    """Agent dependency that analyzes risk for one user turn."""

    async def analyze(self, payload: RiskInput) -> RiskResult:
        """Return a typed risk result."""


class StateDeltaExtractor(Protocol):
    """Agent dependency that extracts state updates from one user turn."""

    async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
        """Return a typed state delta."""


class FeedbackAnalyzer(Protocol):
    """Agent dependency that evaluates one pending intervention."""

    async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
        """Return typed observable feedback for the pending intervention."""


class StrategySelector(Protocol):
    """Agent dependency that selects the next dialogue strategy."""

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Return a typed strategy plan."""


class TurnOrchestrator:
    """Coordinates one user turn across agents, services, and repositories."""

    def __init__(
        self,
        risk_agent: RiskAnalyzer,
        state_tracker: StateDeltaExtractor,
        feedback_evaluator: FeedbackAnalyzer,
        strategy_planner: StrategySelector,
        response_agent: FakeResponseAgent,
        output_guard: FakeOutputGuard,
        state_reducer: StateReducer,
        context_builder: ContextBuilder,
        memory_retriever: MemoryRetriever,
        message_repository: MessageRepository,
        state_repository: StateRepository,
        summary_repository: SummaryRepository,
        intervention_repository: InterventionRepository,
        task_queue: TaskQueue,
        safety_router: SafetyRouter | None = None,
    ) -> None:
        """Store the typed dependencies needed to process a turn."""

        self._risk_agent = risk_agent
        self._state_tracker = state_tracker
        self._feedback_evaluator = feedback_evaluator
        self._strategy_planner = strategy_planner
        self._response_agent = response_agent
        self._output_guard = output_guard
        self._state_reducer = state_reducer
        self._context_builder = context_builder
        self._memory_retriever = memory_retriever
        self._message_repository = message_repository
        self._state_repository = state_repository
        self._summary_repository = summary_repository
        self._intervention_repository = intervention_repository
        self._task_queue = task_queue
        self._safety_router = safety_router or SafetyRouter()

    async def handle_turn(
        self,
        user_id: UserId,
        session_id: SessionId,
        text: str,
    ) -> ChatTurnResult:
        """Process one user message and return the final assistant response."""

        user_message = await self._message_repository.create_user_message(
            session_id=session_id,
            text=text,
        )
        previous_state = await self._state_repository.get_current(session_id)
        feedback = await self._evaluate_pending_intervention(session_id, user_message)
        recent_for_risk = await self._message_repository.get_recent(session_id, limit=8)
        risk_result = await self._risk_agent.analyze(
            RiskInput(
                current_message=user_message,
                recent_messages=recent_for_risk,
                current_risk_level=previous_state.risk_state.level,
            )
        )
        state_delta = await self._state_tracker.extract_delta(
            StateTrackerInput(
                current_message=user_message,
                recent_messages=recent_for_risk,
                previous_state=previous_state,
                feedback=feedback,
            )
        )
        current_state = self._state_reducer.apply(
            previous_state=previous_state,
            delta=state_delta,
            feedback=feedback,
            risk=risk_result,
        )
        await self._state_repository.save_version(
            current_state,
            source_message_id=user_message.id,
        )

        if risk_result.route != RiskRoute.NORMAL:
            return await self._handle_safety_route(session_id, current_state.version, risk_result)

        memories = await self._memory_retriever.retrieve(
            user_id=str(user_id),
            query=text,
            session_state=current_state,
        )
        strategy = await self._strategy_planner.plan(
            StrategyPlannerInput(
                session_state=current_state,
                feedback=feedback,
                memories=memories,
                risk=risk_result,
            )
        )
        context = await self._context_builder.build(
            session_state=current_state,
            rolling_summary=await self._summary_repository.get_current(session_id),
            recent_messages=await self._message_repository.get_recent(session_id, limit=12),
            memories=memories,
            strategy=strategy,
            risk=risk_result,
        )
        draft = await self._response_agent.generate(context)
        guard_result = await self._output_guard.review(
            GuardInput(draft=draft, context=context, risk=risk_result)
        )
        final_text = guard_result.final_text(draft)
        assistant_message = await self._message_repository.create_assistant_message(
            session_id=session_id,
            text=final_text,
        )
        await self._intervention_repository.create_pending(
            session_id=session_id,
            assistant_message_id=assistant_message.id,
            strategy=strategy.primary_strategy.value,
            objective=strategy.objective,
            expected_signals=strategy.expected_signals,
        )
        await self._task_queue.enqueue(
            "post_turn",
            user_id=str(user_id),
            session_id=str(session_id),
        )
        return ChatTurnResult(
            message_id=assistant_message.id,
            response=final_text,
            session_id=session_id,
            state_version=current_state.version,
        )

    async def _evaluate_pending_intervention(
        self,
        session_id: SessionId,
        user_message: Message,
    ) -> FeedbackResult | None:
        """Evaluate and complete the previous intervention when one exists."""

        pending = await self._intervention_repository.get_pending(session_id)
        if pending is None:
            return None
        assistant_message = await self._message_repository.get_by_id(pending.assistant_message_id)
        if assistant_message is None:
            return None
        feedback = await self._feedback_evaluator.evaluate(
            FeedbackInput(
                previous_intervention=pending,
                assistant_message=assistant_message,
                next_user_message=user_message,
            )
        )
        await self._intervention_repository.complete(pending.intervention_id, feedback)
        return feedback

    async def _handle_safety_route(
        self,
        session_id: SessionId,
        state_version: int,
        risk_result: RiskResult,
    ) -> ChatTurnResult:
        """Persist and return a controlled safety response."""

        safety_text = await self._safety_router.handle(risk_result)
        assistant_message = await self._message_repository.create_assistant_message(
            session_id=session_id,
            text=safety_text,
        )
        return ChatTurnResult(
            message_id=assistant_message.id,
            response=safety_text,
            session_id=session_id,
            state_version=state_version,
        )
