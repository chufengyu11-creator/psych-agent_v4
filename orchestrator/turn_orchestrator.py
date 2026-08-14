# """Single-turn orchestration for the psychological support agent."""

# from __future__ import annotations

# from typing import Protocol

# from orchestrator.post_turn_pipeline import TaskQueue
# from orchestrator.safety_router import SafetyRouter
# from schemas.common import SessionId, UserId
# from schemas.context import ResponseContext
# from schemas.feedback import FeedbackInput, FeedbackResult
# from schemas.memory import RetrievedMemories
# from schemas.messages import ChatTurnResult, Message
# from schemas.risk import RiskInput, RiskResult, RiskRoute
# from schemas.safety import DraftResponse, GuardInput, GuardResult
# from schemas.state import SessionState, StateDelta, StateTrackerInput
# from schemas.strategy import StrategyPlan, StrategyPlannerInput
# from services.context_builder import ContextBuilder
# from services.state_reducer import StateReducer
# from storage.repositories.intervention_repository import InterventionRepository
# from storage.repositories.message_repository import MessageRepository
# from storage.repositories.state_repository import StateRepository
# from storage.repositories.summary_repository import SummaryRepository


# class RiskAnalyzer(Protocol):
#     """Agent dependency that analyzes risk for one user turn."""

#     async def analyze(self, payload: RiskInput) -> RiskResult:
#         """Return a typed risk result."""


# class StateDeltaExtractor(Protocol):
#     """Agent dependency that extracts state updates from one user turn."""

#     async def extract_delta(self, payload: StateTrackerInput) -> StateDelta:
#         """Return a typed state delta."""


# class StrategySelector(Protocol):
#     """Agent dependency that selects the next dialogue strategy."""

#     async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
#         """Return a typed strategy plan."""


# class FeedbackAnalyzer(Protocol):
#     """Agent dependency that evaluates user feedback on a pending intervention."""

#     async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
#         """Return a typed feedback result."""


# class ResponseGenerator(Protocol):
#     """Agent dependency that drafts one user-facing response."""

#     async def generate(self, context: ResponseContext) -> DraftResponse:
#         """Return a typed draft response."""


# class OutputReviewer(Protocol):
#     """Agent dependency that reviews a draft before it can be sent."""

#     async def review(self, payload: GuardInput) -> GuardResult:
#         """Return a typed guard decision."""


# class MemoryRetrieverProtocol(Protocol):
#     """Service dependency that retrieves cross-session memory context."""

#     async def retrieve(
#         self,
#         user_id: str,
#         query: str,
#         session_state: SessionState,
#         limit: int = 8,
#     ) -> RetrievedMemories:
#         """Return retrieved memories for this user turn."""


# class TurnOrchestrator:
#     """Coordinates one user turn across agents, services, and repositories."""

#     def __init__(
#         self,
#         risk_agent: RiskAnalyzer,
#         state_tracker: StateDeltaExtractor,
#         feedback_evaluator: FeedbackAnalyzer,
#         strategy_planner: StrategySelector,
#         response_agent: ResponseGenerator,
#         output_guard: OutputReviewer,
#         state_reducer: StateReducer,
#         context_builder: ContextBuilder,
#         memory_retriever: MemoryRetrieverProtocol,
#         message_repository: MessageRepository,
#         state_repository: StateRepository,
#         summary_repository: SummaryRepository,
#         intervention_repository: InterventionRepository,
#         task_queue: TaskQueue,
#         safety_router: SafetyRouter | None = None,
#     ) -> None:
#         """Store the typed dependencies needed to process a turn."""

#         self._risk_agent = risk_agent
#         self._state_tracker = state_tracker
#         self._feedback_evaluator = feedback_evaluator
#         self._strategy_planner = strategy_planner
#         self._response_agent = response_agent
#         self._output_guard = output_guard
#         self._state_reducer = state_reducer
#         self._context_builder = context_builder
#         self._memory_retriever = memory_retriever
#         self._message_repository = message_repository
#         self._state_repository = state_repository
#         self._summary_repository = summary_repository
#         self._intervention_repository = intervention_repository
#         self._task_queue = task_queue
#         self._safety_router = safety_router or SafetyRouter()

#     async def handle_turn(
#         self,
#         user_id: UserId,
#         session_id: SessionId,
#         text: str,
#     ) -> ChatTurnResult:
#         """Process one user message and return the final assistant response."""

#         user_message = await self._message_repository.create_user_message(
#             user_id=user_id,
#             session_id=session_id,
#             text=text,
#         )
#         previous_state = await self._state_repository.get_current(user_id, session_id)
#         feedback = await self._evaluate_pending_intervention(
#             user_id,
#             session_id,
#             user_message,
#         )
#         recent_for_risk = await self._message_repository.get_recent(
#             user_id,
#             session_id,
#             limit=8,
#         )
#         risk_result = await self._risk_agent.analyze(
#             RiskInput(
#                 current_message=user_message,
#                 recent_messages=recent_for_risk,
#                 current_risk_level=previous_state.risk_state.level,
#             )
#         )
#         state_delta = await self._state_tracker.extract_delta(
#             StateTrackerInput(
#                 current_message=user_message,
#                 recent_messages=recent_for_risk,
#                 previous_state=previous_state,
#                 feedback=feedback,
#             )
#         )
#         current_state = self._state_reducer.apply(
#             previous_state=previous_state,
#             delta=state_delta,
#             feedback=feedback,
#             risk=risk_result,
#         )
#         await self._state_repository.save_version(
#             user_id,
#             current_state,
#             source_message_id=user_message.id,
#         )

#         if risk_result.route != RiskRoute.NORMAL:
#             return await self._handle_safety_route(
#                 user_id,
#                 session_id,
#                 current_state.version,
#                 risk_result,
#             )

#         memories = await self._memory_retriever.retrieve(
#             user_id=str(user_id),
#             query=text,
#             session_state=current_state,
#         )
#         strategy = await self._strategy_planner.plan(
#             StrategyPlannerInput(
#                 session_state=current_state,
#                 feedback=feedback,
#                 memories=memories,
#                 risk=risk_result,
#             )
#         )
#         context = await self._context_builder.build(
#             session_state=current_state,
#             rolling_summary=await self._summary_repository.get_current(
#                 user_id,
#                 session_id,
#             ),
#             recent_messages=await self._message_repository.get_recent(
#                 user_id,
#                 session_id,
#                 limit=12,
#             ),
#             memories=memories,
#             strategy=strategy,
#             risk=risk_result,
#         )
#         draft = await self._response_agent.generate(context)
#         guard_result = await self._output_guard.review(
#             GuardInput(draft=draft, context=context, risk=risk_result)
#         )
#         final_text = guard_result.final_text(draft)
#         assistant_message = await self._message_repository.create_assistant_message(
#             user_id=user_id,
#             session_id=session_id,
#             text=final_text,
#         )
#         await self._intervention_repository.create_pending(
#             user_id=user_id,
#             session_id=session_id,
#             assistant_message_id=assistant_message.id,
#             strategy=strategy.primary_strategy.value,
#             objective=strategy.objective,
#             expected_signals=strategy.expected_signals,
#         )
#         await self._task_queue.enqueue(
#             "post_turn",
#             user_id=str(user_id),
#             session_id=str(session_id),
#         )
#         return ChatTurnResult(
#             message_id=assistant_message.id,
#             response=final_text,
#             session_id=session_id,
#             state_version=current_state.version,
#         )

#     async def _evaluate_pending_intervention(
#         self,
#         user_id: UserId,
#         session_id: SessionId,
#         user_message: Message,
#     ) -> FeedbackResult | None:
#         """Evaluate and complete the previous intervention when one exists."""

#         pending = await self._intervention_repository.get_pending(user_id, session_id)
#         if pending is None:
#             return None
#         assistant_message = await self._message_repository.get_by_id(
#             user_id,
#             pending.assistant_message_id,
#         )
#         if assistant_message is None:
#             return None
#         feedback = await self._feedback_evaluator.evaluate(
#             FeedbackInput(
#                 previous_intervention=pending,
#                 assistant_message=assistant_message,
#                 next_user_message=user_message,
#             )
#         )
#         await self._intervention_repository.complete(
#             user_id,
#             session_id,
#             pending.intervention_id,
#             feedback,
#         )
#         return feedback

#     async def _handle_safety_route(
#         self,
#         user_id: UserId,
#         session_id: SessionId,
#         state_version: int,
#         risk_result: RiskResult,
#     ) -> ChatTurnResult:
#         """Persist and return a controlled safety response."""

#         safety_text = await self._safety_router.handle(risk_result)
#         assistant_message = await self._message_repository.create_assistant_message(
#             user_id=user_id,
#             session_id=session_id,
#             text=safety_text,
#         )
#         return ChatTurnResult(
#             message_id=assistant_message.id,
#             response=safety_text,
#             session_id=session_id,
#             state_version=state_version,
#         )



"""Single-turn orchestration for the psychological support agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from orchestrator.deep_state_pipeline import DeepStateTask, DeepStateTaskQueue
from orchestrator.post_turn_pipeline import TaskQueue
from orchestrator.safety_router import SafetyRouter
from schemas.common import SessionId, UserId
from schemas.context import ResponseContext
from schemas.feedback import FeedbackInput, FeedbackResult
from schemas.knowledge import RetrievedKnowledge
from schemas.memory import RetrievedMemories
from schemas.messages import ChatTurnResult, Message
from schemas.risk import RiskInput, RiskResult, RiskRoute
from schemas.safety import DraftResponse, GuardInput, GuardResult
from schemas.state import SessionState, StateDelta, StateTrackerInput
from schemas.strategy import StrategyPlan, StrategyPlannerInput
from services.context_builder import ContextBuilder
from services.knowledge_retriever import DisabledKnowledgeRetriever
from services.memory_query import detect_memory_query_intent
from services.state_reducer import StateReducer
from services.timing import timed_awaitable, timing_span
from storage.repositories.deep_state_repository import DeepStateRepository
from storage.repositories.knowledge_repository import (
    KnowledgeUsageRepository,
    NoopKnowledgeUsageRepository,
)
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


class StrategySelector(Protocol):
    """Agent dependency that selects the next dialogue strategy."""

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Return a typed strategy plan."""


class FeedbackAnalyzer(Protocol):
    """Agent dependency that evaluates user feedback on a pending intervention."""

    async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
        """Return a typed feedback result."""


class ResponseGenerator(Protocol):
    """Agent dependency that drafts one user-facing response."""

    async def generate(self, context: ResponseContext) -> DraftResponse:
        """Return a typed draft response."""


class OutputReviewer(Protocol):
    """Agent dependency that reviews a draft before it can be sent."""

    async def review(self, payload: GuardInput) -> GuardResult:
        """Return a typed guard decision."""


class MemoryRetrieverProtocol(Protocol):
    """Service dependency that retrieves cross-session memory context."""

    async def retrieve(
        self,
        user_id: str,
        query: str,
        session_state: SessionState,
        limit: int = 8,
    ) -> RetrievedMemories:
        """Return retrieved memories for this user turn."""


class KnowledgeRetrieverDependency(Protocol):
    """Retrieve reviewed public evidence without user-private inputs."""

    async def retrieve(self, query: str) -> RetrievedKnowledge:
        """Return a bounded evidence set for this turn."""


class TurnOrchestrator:
    """Coordinates one user turn across agents, services, and repositories."""

    def __init__(
        self,
        risk_agent: RiskAnalyzer,
        state_tracker: StateDeltaExtractor,
        feedback_evaluator: FeedbackAnalyzer,
        strategy_planner: StrategySelector,
        response_agent: ResponseGenerator,
        output_guard: OutputReviewer,
        state_reducer: StateReducer,
        context_builder: ContextBuilder,
        memory_retriever: MemoryRetrieverProtocol,
        message_repository: MessageRepository,
        state_repository: StateRepository,
        summary_repository: SummaryRepository,
        intervention_repository: InterventionRepository,
        task_queue: TaskQueue,
        knowledge_retriever: KnowledgeRetrieverDependency | None = None,
        knowledge_usage_repository: KnowledgeUsageRepository | None = None,
        fast_state_tracker: StateDeltaExtractor | None = None,
        deep_state_task_queue: DeepStateTaskQueue | None = None,
        deep_state_repository: DeepStateRepository | None = None,
        safety_router: SafetyRouter | None = None,
    ) -> None:
        """Store the typed dependencies needed to process a turn."""

        self._risk_agent = risk_agent
        self._state_tracker = state_tracker
        self._fast_state_tracker = fast_state_tracker
        self._deep_state_task_queue = deep_state_task_queue
        self._deep_state_repository = deep_state_repository
        self._feedback_evaluator = feedback_evaluator
        self._strategy_planner = strategy_planner
        self._response_agent = response_agent
        self._output_guard = output_guard
        self._state_reducer = state_reducer
        self._context_builder = context_builder
        self._memory_retriever = memory_retriever
        self._knowledge_retriever = knowledge_retriever or DisabledKnowledgeRetriever()
        self._knowledge_usage_repository = (
            knowledge_usage_repository or NoopKnowledgeUsageRepository()
        )
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
        nonverbal_observations: dict[str, object] | None = None,
    ) -> ChatTurnResult:
        """Process one user message and return the final assistant response."""

        with timing_span("db.create_user_message"):
            user_message = await self._message_repository.create_user_message(
                user_id=user_id,
                session_id=session_id,
                text=text,
            )
        with timing_span("db.load_previous_state"):
            previous_state = await self._state_repository.get_current(
                user_id,
                session_id,
            )
        with timing_span("turn.feedback_evaluation"):
            feedback = await self._evaluate_pending_intervention(
                user_id,
                session_id,
                user_message,
            )
        with timing_span("db.load_recent_for_risk"):
            recent_for_risk = await self._message_repository.get_recent(
                user_id,
                session_id,
                limit=8,
            )
        risk_input = RiskInput(
            current_message=user_message,
            recent_messages=recent_for_risk,
            current_risk_level=previous_state.risk_state.level,
        )
        state_input = StateTrackerInput(
            current_message=user_message,
            recent_messages=recent_for_risk,
            previous_state=previous_state,
            feedback=feedback,
        )
        active_state_tracker = self._fast_state_tracker or self._state_tracker
        state_stage = (
            "agent.fast_state_tracker"
            if self._fast_state_tracker is not None
            else "agent.state_tracker"
        )
        with timing_span("agents.risk_state_parallel"):
            risk_result, state_delta = await asyncio.gather(
                timed_awaitable(
                    "agent.risk",
                    self._risk_agent.analyze(risk_input),
                ),
                timed_awaitable(
                    state_stage,
                    active_state_tracker.extract_delta(state_input),
                ),
            )
        deep_candidate = None
        state_base = previous_state
        if self._deep_state_repository is not None:
            with timing_span("db.load_latest_deep_state"):
                deep_candidate = await self._deep_state_repository.get_latest_ready(
                    user_id,
                    session_id,
                    before_sequence=user_message.sequence_number,
                )
        if deep_candidate is not None:
            with timing_span("state.apply_deep_state"):
                state_base = self._state_reducer.apply(
                    previous_state=previous_state,
                    delta=deep_candidate.delta,
                    feedback=None,
                    risk=risk_result,
                )
                state_base.version = previous_state.version
        with timing_span("state.reduce"):
            current_state = self._state_reducer.apply(
                previous_state=state_base,
                delta=state_delta,
                feedback=feedback,
                risk=risk_result,
            )
        with timing_span("db.save_state"):
            await self._state_repository.save_version(
                user_id,
                current_state,
                source_message_id=user_message.id,
            )

        if deep_candidate is not None and self._deep_state_repository is not None:
            with timing_span("db.mark_deep_state_applied"):
                marked = await self._deep_state_repository.mark_applied(
                    deep_candidate.id
                )
                if not marked:
                    raise RuntimeError("ready deep-state result could not be applied")
                await self._deep_state_repository.expire_older_ready(
                    user_id,
                    session_id,
                    before_sequence=deep_candidate.source_message_sequence,
                )

        if _requires_hard_safety_route(risk_result):
            with timing_span("turn.safety_route"):
                return await self._handle_safety_route(
                    user_id,
                    session_id,
                    current_state.version,
                    risk_result,
                )

        response_risk = _nonblocking_response_risk(risk_result)
        memory_query_intent = detect_memory_query_intent(text)
        with timing_span("memory.retrieve"):
            memories = await self._memory_retriever.retrieve(
                user_id=str(user_id),
                query=text,
                session_state=current_state,
            )
        with timing_span("rag.retrieve"):
            knowledge = await self._knowledge_retriever.retrieve(text)
        with timing_span("agent.strategy"):
            strategy = await self._strategy_planner.plan(
                StrategyPlannerInput(
                    session_state=current_state,
                    feedback=feedback,
                    memories=memories,
                    knowledge=knowledge,
                    risk=response_risk,
                    current_user_message=text,
                    memory_query_intent=memory_query_intent,
                )
            )
        if self._deep_state_task_queue is not None:
            with timing_span("deep_state.enqueue"):
                await self._deep_state_task_queue.enqueue(
                    DeepStateTask(
                        user_id=str(user_id),
                        session_id=str(session_id),
                        source_message_id=str(user_message.id),
                        source_message_sequence=user_message.sequence_number,
                        base_state_version=current_state.version,
                        payload=state_input.model_copy(
                            update={"previous_state": current_state},
                        ),
                    )
                )
        with timing_span("db.load_rolling_summary"):
            rolling_summary = await self._summary_repository.get_current(
                user_id,
                session_id,
            )
        with timing_span("db.load_recent_for_context"):
            recent_messages = await self._message_repository.get_recent(
                user_id,
                session_id,
                limit=12,
            )
        with timing_span("context.build"):
            context = await self._context_builder.build(
                session_state=current_state,
                rolling_summary=rolling_summary,
                recent_messages=recent_messages,
                memories=memories,
                knowledge=knowledge,
                strategy=strategy,
                risk=response_risk,
                current_user_message=text,
                nonverbal_observations=nonverbal_observations,
                memory_query_intent=memory_query_intent,
            )
        with timing_span("agent.response"):
            draft = await self._response_agent.generate(context)
        with timing_span("agent.output_guard"):
            guard_result = await self._output_guard.review(
                GuardInput(draft=draft, context=context, risk=response_risk)
            )
        final_text = guard_result.final_text(draft)
        with timing_span("db.save_assistant_message"):
            assistant_message = (
                await self._message_repository.create_assistant_message(
                    user_id=user_id,
                    session_id=session_id,
                    text=final_text,
                )
            )
        try:
            with timing_span("rag.record_usage"):
                await self._knowledge_usage_repository.record(
                    str(assistant_message.id),
                    knowledge,
                    (
                        draft.referenced_knowledge_ids
                        if guard_result.decision.value == "allow"
                        else []
                    ),
                    None,
                )
        except Exception:
            logging.getLogger(__name__).exception("knowledge usage audit failed")
        with timing_span("db.save_intervention"):
            await self._intervention_repository.create_pending(
                user_id=user_id,
                session_id=session_id,
                assistant_message_id=assistant_message.id,
                strategy=strategy.primary_strategy.value,
                objective=strategy.objective,
                expected_signals=strategy.expected_signals,
            )
        with timing_span("post_turn.enqueue"):
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
        user_id: UserId,
        session_id: SessionId,
        user_message: Message,
    ) -> FeedbackResult | None:
        """Evaluate and complete the previous intervention when one exists."""

        pending = await self._intervention_repository.get_pending(user_id, session_id)
        if pending is None:
            return None
        assistant_message = await self._message_repository.get_by_id(
            user_id,
            pending.assistant_message_id,
        )
        if assistant_message is None:
            return None
        feedback = await self._feedback_evaluator.evaluate(
            FeedbackInput(
                previous_intervention=pending,
                assistant_message=assistant_message,
                next_user_message=user_message,
            )
        )
        await self._intervention_repository.complete(
            user_id,
            session_id,
            pending.intervention_id,
            feedback,
        )
        return feedback

    async def _handle_safety_route(
        self,
        user_id: UserId,
        session_id: SessionId,
        state_version: int,
        risk_result: RiskResult,
    ) -> ChatTurnResult:
        """Persist and return a controlled safety response."""

        safety_text = await self._safety_router.handle(risk_result)
        assistant_message = await self._message_repository.create_assistant_message(
            user_id=user_id,
            session_id=session_id,
            text=safety_text,
        )
        return ChatTurnResult(
            message_id=assistant_message.id,
            response=safety_text,
            session_id=session_id,
            state_version=state_version,
        )

def _requires_hard_safety_route(risk_result: RiskResult) -> bool:
    """Only explicit high-risk routes should interrupt the normal response path."""

    return risk_result.route in {RiskRoute.CRISIS, RiskRoute.HUMAN}


def _nonblocking_response_risk(risk_result: RiskResult) -> RiskResult:
    """Keep soft risk in state while avoiding safety-lock loops during response."""

    if risk_result.route == RiskRoute.CLARIFICATION:
        return risk_result.model_copy(
            update={"route": RiskRoute.NORMAL, "needs_clarification": False}
        )
    return risk_result
