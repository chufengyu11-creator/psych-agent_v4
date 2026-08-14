# """Database-backed turn runner that opens one transaction per user turn."""

# from typing import Protocol

# from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# from agents.feedback_evaluator import FakeFeedbackEvaluator
# from agents.output_guard import FakeOutputGuard
# from agents.response_agent import FakeResponseAgent
# from agents.risk_agent import FakeRiskAgent
# from agents.state_tracker import FakeStateTracker
# from agents.strategy_planner import FakeStrategyPlanner
# from orchestrator.post_turn_pipeline import NoopTaskQueue, TaskQueue
# from orchestrator.turn_orchestrator import (
#     FeedbackAnalyzer,
#     OutputReviewer,
#     ResponseGenerator,
#     RiskAnalyzer,
#     StateDeltaExtractor,
#     StrategySelector,
#     TurnOrchestrator,
# )
# from schemas.common import SessionId, UserId
# from schemas.messages import ChatTurnResult
# from services.context_builder import ContextBuilder
# from services.memory_retriever import RepositoryMemoryRetriever
# from services.state_reducer import StateReducer
# from storage.database import transactional_session
# from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
# from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
# from storage.repositories.message_repository import SqlAlchemyMessageRepository
# from storage.repositories.session_repository import SqlAlchemySessionRepository
# from storage.repositories.state_repository import SqlAlchemyStateRepository
# from storage.repositories.summary_repository import SqlAlchemySummaryRepository
# from storage.repositories.user_repository import SqlAlchemyUserRepository


# class TaskQueueFactory(Protocol):
#     """Build a task queue bound to the current transaction session."""

#     def __call__(self, session: AsyncSession) -> TaskQueue:
#         """Return a queue that can observe writes in the supplied session."""


# class SqlAlchemyTurnOrchestrator:
#     """Runs TurnOrchestrator with SQLAlchemy repositories in one transaction."""

#     def __init__(
#         self,
#         session_factory: async_sessionmaker[AsyncSession],
#         task_queue: TaskQueue | None = None,
#         *,
#         task_queue_factory: TaskQueueFactory | None = None,
#         risk_agent: RiskAnalyzer | None = None,
#         state_tracker: StateDeltaExtractor | None = None,
#         strategy_planner: StrategySelector | None = None,
#         feedback_evaluator: FeedbackAnalyzer | None = None,
#         response_agent: ResponseGenerator | None = None,
#         output_guard: OutputReviewer | None = None,
#     ) -> None:
#         """Store the factory used to create one session per turn."""

#         if task_queue is not None and task_queue_factory is not None:
#             raise ValueError("task_queue and task_queue_factory are mutually exclusive")
#         self._session_factory = session_factory
#         self._task_queue = task_queue if task_queue is not None else NoopTaskQueue()
#         self._task_queue_factory = task_queue_factory
#         self._risk_agent = risk_agent or FakeRiskAgent()
#         self._state_tracker = state_tracker or FakeStateTracker()
#         self._strategy_planner = strategy_planner or FakeStrategyPlanner()
#         self._feedback_evaluator = feedback_evaluator or FakeFeedbackEvaluator()
#         self._response_agent = response_agent or FakeResponseAgent()
#         self._output_guard = output_guard or FakeOutputGuard()

#     @property
#     def risk_agent(self) -> RiskAnalyzer:
#         """Return the configured risk dependency for diagnostics and tests."""

#         return self._risk_agent

#     @property
#     def state_tracker(self) -> StateDeltaExtractor:
#         """Return the configured state dependency for diagnostics and tests."""

#         return self._state_tracker

#     @property
#     def strategy_planner(self) -> StrategySelector:
#         """Return the configured strategy dependency for diagnostics and tests."""

#         return self._strategy_planner

#     @property
#     def feedback_evaluator(self) -> FeedbackAnalyzer:
#         """Return the configured feedback evaluator."""

#         return self._feedback_evaluator

#     @property
#     def response_agent(self) -> ResponseGenerator:
#         """Return the configured response agent."""

#         return self._response_agent

#     @property
#     def output_guard(self) -> OutputReviewer:
#         """Return the configured output guard."""

#         return self._output_guard

#     @property
#     def task_queue(self) -> TaskQueue:
#         """Return the configured post-turn queue boundary."""

#         return self._task_queue

#     async def handle_turn(
#         self,
#         user_id: UserId,
#         session_id: SessionId,
#         text: str,
#     ) -> ChatTurnResult:
#         """Ensure user/session rows and process one turn inside a transaction."""

#         async with transactional_session(self._session_factory) as session:
#             await SqlAlchemyUserRepository(session).ensure_user(user_id)
#             await SqlAlchemySessionRepository(session).ensure_session(
#                 user_id,
#                 session_id,
#             )
#             orchestrator = self._build_turn_orchestrator(session)
#             return await orchestrator.handle_turn(
#                 user_id=user_id,
#                 session_id=session_id,
#                 text=text,
#             )

#     def _build_turn_orchestrator(self, session: AsyncSession) -> TurnOrchestrator:
#         """Build the inner orchestrator with repositories bound to this session."""

#         return TurnOrchestrator(
#             risk_agent=self._risk_agent,
#             state_tracker=self._state_tracker,
#             feedback_evaluator=self._feedback_evaluator,
#             strategy_planner=self._strategy_planner,
#             response_agent=self._response_agent,
#             output_guard=self._output_guard,
#             state_reducer=StateReducer(),
#             context_builder=ContextBuilder(),
#             memory_retriever=RepositoryMemoryRetriever(
#                 SqlAlchemyMemoryRepository(session)
#             ),
#             message_repository=SqlAlchemyMessageRepository(session),
#             state_repository=SqlAlchemyStateRepository(session),
#             summary_repository=SqlAlchemySummaryRepository(session),
#             intervention_repository=SqlAlchemyInterventionRepository(session),
#             task_queue=self._task_queue_for(session),
#         )

#     def _task_queue_for(self, session: AsyncSession) -> TaskQueue:
#         """Return a per-transaction queue or the configured static queue."""

#         if self._task_queue_factory is not None:
#             return self._task_queue_factory(session)
#         return self._task_queue


# __all__ = ["SqlAlchemyTurnOrchestrator", "TaskQueueFactory"]











"""Database-backed turn runner that opens one transaction per user turn."""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from orchestrator.deep_state_pipeline import DeepStateTask, DeepStateTaskQueue
from orchestrator.post_turn_pipeline import (
    BufferedPostTurnTaskQueue,
    NoopTaskQueue,
    TaskQueue,
)
from orchestrator.turn_orchestrator import (
    FeedbackAnalyzer,
    OutputReviewer,
    ResponseGenerator,
    RiskAnalyzer,
    StateDeltaExtractor,
    StrategySelector,
    TurnOrchestrator,
)
from schemas.common import SessionId, UserId
from schemas.messages import ChatTurnResult
from services.context_builder import ContextBuilder
from services.knowledge_retriever import (
    DisabledKnowledgeRetriever,
    KnowledgeRetrieverProtocol,
)
from services.memory_retriever import RepositoryMemoryRetriever
from services.state_reducer import StateReducer
from services.token_budget import TokenBudgetManager
from services.timing import timing_span
from storage.database import transactional_session
from storage.repositories.deep_state_repository import SqlAlchemyDeepStateRepository
from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
from storage.repositories.knowledge_repository import SqlAlchemyKnowledgeRepository
from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
from storage.repositories.message_repository import SqlAlchemyMessageRepository
from storage.repositories.session_repository import SqlAlchemySessionRepository
from storage.repositories.state_repository import SqlAlchemyStateRepository
from storage.repositories.summary_repository import SqlAlchemySummaryRepository
from storage.repositories.user_repository import SqlAlchemyUserRepository


class TaskQueueFactory(Protocol):
    """Build a task queue bound to the current transaction session."""

    def __call__(self, session: AsyncSession) -> TaskQueue:
        """Return a queue that can observe writes in the supplied session."""

class _TransactionDeepStateTaskQueue:
    """Track source messages so background persistence follows transaction outcome."""

    def __init__(self, delegate: DeepStateTaskQueue) -> None:
        self._delegate = delegate
        self._source_message_ids: list[str] = []

    async def enqueue(self, task: DeepStateTask) -> None:
        await self._delegate.enqueue(task)
        self._source_message_ids.append(task.source_message_id)

    def mark_committed(self, source_message_id: str) -> None:
        self._delegate.mark_committed(source_message_id)

    def mark_rolled_back(self, source_message_id: str) -> None:
        self._delegate.mark_rolled_back(source_message_id)

    async def wait_for_session(self, user_id: str, session_id: str) -> None:
        await self._delegate.wait_for_session(user_id, session_id)

    async def aclose(self) -> None:
        """The application, not one request, owns the delegate lifecycle."""

    def mark_all_committed(self) -> None:
        for source_message_id in self._source_message_ids:
            self._delegate.mark_committed(source_message_id)

    def mark_all_rolled_back(self) -> None:
        for source_message_id in self._source_message_ids:
            self._delegate.mark_rolled_back(source_message_id)



class SqlAlchemyTurnOrchestrator:
    """Runs TurnOrchestrator with SQLAlchemy repositories in one transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_queue: TaskQueue | None = None,
        *,
        task_queue_factory: TaskQueueFactory | None = None,
        post_commit_task_queue: TaskQueue | None = None,
        risk_agent: RiskAnalyzer | None = None,
        state_tracker: StateDeltaExtractor | None = None,
        fast_state_tracker: StateDeltaExtractor | None = None,
        deep_state_task_queue: DeepStateTaskQueue | None = None,
        strategy_planner: StrategySelector | None = None,
        feedback_evaluator: FeedbackAnalyzer | None = None,
        response_agent: ResponseGenerator | None = None,
        output_guard: OutputReviewer | None = None,
        knowledge_retriever: KnowledgeRetrieverProtocol | None = None,
        knowledge_context_tokens: int = 1200,
    ) -> None:
        """Store the factory used to create one session per turn."""

        configured_queues = sum(
            queue is not None
            for queue in (task_queue, task_queue_factory, post_commit_task_queue)
        )
        if configured_queues > 1:
            raise ValueError(
                "task_queue, task_queue_factory, and post_commit_task_queue "
                "are mutually exclusive"
            )
        self._session_factory = session_factory
        self._post_commit_task_queue = post_commit_task_queue
        self._task_queue = (
            post_commit_task_queue
            or task_queue
            or NoopTaskQueue()
        )
        self._task_queue_factory = task_queue_factory
        self._risk_agent = risk_agent or FakeRiskAgent()
        self._state_tracker = state_tracker or FakeStateTracker()
        self._fast_state_tracker = fast_state_tracker
        self._deep_state_task_queue = deep_state_task_queue
        self._strategy_planner = strategy_planner or FakeStrategyPlanner()
        self._feedback_evaluator = feedback_evaluator or FakeFeedbackEvaluator()
        self._response_agent = response_agent or FakeResponseAgent()
        self._output_guard = output_guard or FakeOutputGuard()
        self._knowledge_retriever = knowledge_retriever or DisabledKnowledgeRetriever()
        self._knowledge_context_tokens = knowledge_context_tokens

    @property
    def risk_agent(self) -> RiskAnalyzer:
        """Return the configured risk dependency for diagnostics and tests."""

        return self._risk_agent

    @property
    def state_tracker(self) -> StateDeltaExtractor:
        """Return the configured state dependency for diagnostics and tests."""

        return self._state_tracker

    @property
    def deep_state_task_queue(self) -> DeepStateTaskQueue | None:
        """Return the optional application-owned deep-state queue."""

        return self._deep_state_task_queue

    @property
    def strategy_planner(self) -> StrategySelector:
        """Return the configured strategy dependency for diagnostics and tests."""

        return self._strategy_planner

    @property
    def feedback_evaluator(self) -> FeedbackAnalyzer:
        """Return the configured feedback evaluator."""

        return self._feedback_evaluator

    @property
    def response_agent(self) -> ResponseGenerator:
        """Return the configured response agent."""

        return self._response_agent

    @property
    def output_guard(self) -> OutputReviewer:
        """Return the configured output guard."""

        return self._output_guard

    @property
    def task_queue(self) -> TaskQueue:
        """Return the configured post-turn queue boundary."""

        return self._task_queue

    async def handle_turn(
        self,
        user_id: UserId,
        session_id: SessionId,
        text: str,
        nonverbal_observations: dict[str, object] | None = None,
    ) -> ChatTurnResult:
        """Ensure user/session rows and process one turn inside a transaction."""

        buffered_queue = (
            BufferedPostTurnTaskQueue()
            if self._post_commit_task_queue is not None
            else None
        )
        deep_queue = (
            _TransactionDeepStateTaskQueue(self._deep_state_task_queue)
            if self._deep_state_task_queue is not None
            else None
        )
        try:
            with timing_span("runtime.transaction_total"):
                async with transactional_session(self._session_factory) as session:
                    with timing_span("runtime.ensure_user_session"):
                        await SqlAlchemyUserRepository(session).ensure_user(user_id)
                        await SqlAlchemySessionRepository(session).ensure_session(
                            user_id,
                            session_id,
                        )
                    orchestrator = self._build_turn_orchestrator(
                        session,
                        task_queue=buffered_queue,
                        deep_state_task_queue=deep_queue,
                    )
                    with timing_span("runtime.turn_orchestrator"):
                        result = await orchestrator.handle_turn(
                            user_id=user_id,
                            session_id=session_id,
                            text=text,
                            nonverbal_observations=nonverbal_observations,
                        )
        except BaseException:
            if deep_queue is not None:
                deep_queue.mark_all_rolled_back()
            raise
        if deep_queue is not None:
            deep_queue.mark_all_committed()

        dispatcher = self._post_commit_task_queue
        if buffered_queue is not None and dispatcher is not None:
            with timing_span("post_turn.dispatch"):
                for task in buffered_queue.drain():
                    await dispatcher.enqueue(
                        task.task_name,
                        task.user_id,
                        task.session_id,
                    )
        return result

    def _build_turn_orchestrator(
        self,
        session: AsyncSession,
        *,
        task_queue: TaskQueue | None = None,
        deep_state_task_queue: DeepStateTaskQueue | None = None,
    ) -> TurnOrchestrator:
        """Build the inner orchestrator with repositories bound to this session."""

        return TurnOrchestrator(
            risk_agent=self._risk_agent,
            state_tracker=self._state_tracker,
            feedback_evaluator=self._feedback_evaluator,
            strategy_planner=self._strategy_planner,
            response_agent=self._response_agent,
            output_guard=self._output_guard,
            state_reducer=StateReducer(),
            context_builder=ContextBuilder(
                TokenBudgetManager({"knowledge": self._knowledge_context_tokens})
            ),
            memory_retriever=RepositoryMemoryRetriever(
                SqlAlchemyMemoryRepository(session)
            ),
            message_repository=SqlAlchemyMessageRepository(session),
            state_repository=SqlAlchemyStateRepository(session),
            summary_repository=SqlAlchemySummaryRepository(session),
            intervention_repository=SqlAlchemyInterventionRepository(session),
            knowledge_retriever=self._knowledge_retriever,
            knowledge_usage_repository=SqlAlchemyKnowledgeRepository(session),
            task_queue=task_queue or self._task_queue_for(session),
            fast_state_tracker=self._fast_state_tracker,
            deep_state_task_queue=deep_state_task_queue,
            deep_state_repository=SqlAlchemyDeepStateRepository(session),
        )

    def _task_queue_for(self, session: AsyncSession) -> TaskQueue:
        """Return a per-transaction queue or the configured static queue."""

        if self._task_queue_factory is not None:
            return self._task_queue_factory(session)
        return self._task_queue


__all__ = ["SqlAlchemyTurnOrchestrator", "TaskQueueFactory"]
