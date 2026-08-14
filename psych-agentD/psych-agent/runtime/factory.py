"""Factories for in-memory and application-owned SQLAlchemy runtimes."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agents.feedback_evaluator import FakeFeedbackEvaluator, FeedbackEvaluator
from agents.memory_curator import MemoryCurator
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent, RiskAgent
from agents.rolling_summarizer import FakeRollingSummarizer, RollingSummarizer
from agents.session_finalizer import FakeSessionFinalizer, SessionFinalizer
from agents.state_tracker import FakeStateTracker, StateTracker
from agents.strategy_planner import FakeStrategyPlanner, StrategyPlanner
from app.config import AppRuntimeMode, Settings, get_settings
from llm.client import LLMClient
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import (
    StructuredLLMClient,
    StructuredLLMClientProtocol,
)
from orchestrator.post_turn_pipeline import InlinePostTurnTaskQueue, NoopTaskQueue, TaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from runtime.application import ApplicationRuntime, RuntimeConfigurationError
from runtime.redis_client import (
    RedisClientFactory,
    RedisResource,
    create_redis_client,
)
from runtime.sqlalchemy_orchestrator import SqlAlchemyTurnOrchestrator, TaskQueueFactory
from runtime.sqlalchemy_session_closer import SqlAlchemySessionCloser
from services.context_builder import ContextBuilder
from services.memory_policy import MemoryPolicy
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.database import create_engine, create_session_factory
from storage.repositories.intervention_repository import (
    InMemoryInterventionRepository,
    SqlAlchemyInterventionRepository,
)
from storage.repositories.message_repository import (
    InMemoryMessageRepository,
    SqlAlchemyMessageRepository,
)
from storage.repositories.state_repository import InMemoryStateRepository, SqlAlchemyStateRepository
from storage.repositories.summary_repository import (
    InMemorySummaryRepository,
    SqlAlchemySummaryRepository,
)
from workers.post_turn_worker import PostTurnWorker
from workers.session_close_worker import MemoryCuratorProtocol
from workers.session_close_worker import SessionFinalizer as SessionFinalizerProtocol
from workers.summary_worker import Summarizer, SummaryWorker

EngineFactory = Callable[[Settings], AsyncEngine]
HTTPClientFactory = Callable[[Settings], OpenAICompatibleHTTPClient]
StructuredClientWrapper = Callable[
    [StructuredLLMClient, OpenAICompatibleHTTPClient],
    StructuredLLMClientProtocol,
]

_RUNTIME_MODES = frozenset({"in_memory", "sqlalchemy_fake", "sqlalchemy_model"})
_shared_orchestrator: TurnOrchestrator | None = None


def build_in_memory_orchestrator() -> TurnOrchestrator:
    """Construct the deterministic in-memory pipeline used by tests."""

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


def build_model_assisted_in_memory_orchestrator(
    llm_client: LLMClient,
    *,
    model_name: str | None = None,
) -> TurnOrchestrator:
    """Construct an in-memory pipeline with model-backed structured agents."""

    structured_client = StructuredLLMClient(
        llm_client,
        default_model_name=model_name or "local-psych-support",
    )
    return TurnOrchestrator(
        risk_agent=RiskAgent(structured_client, model_name=model_name),
        state_tracker=StateTracker(structured_client, model_name=model_name),
        feedback_evaluator=FeedbackEvaluator(
            structured_client,
            model_name=model_name,
        ),
        strategy_planner=StrategyPlanner(structured_client, model_name=model_name),
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


def build_sqlalchemy_orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyTurnOrchestrator:
    """Construct a database-backed turn runner with deterministic agents."""

    return SqlAlchemyTurnOrchestrator(session_factory=session_factory)


def build_sqlalchemy_orchestrator_with_post_turn_summary(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    summarizer: Summarizer | None = None,
) -> SqlAlchemyTurnOrchestrator:
    """Build a SQLAlchemy turn runner whose post-turn queue updates summaries."""

    return SqlAlchemyTurnOrchestrator(
        session_factory=session_factory,
        task_queue_factory=_post_turn_task_queue_factory(summarizer or FakeRollingSummarizer()),
    )


def build_model_assisted_sqlalchemy_orchestrator(
    session_factory: async_sessionmaker[AsyncSession],
    structured_client: StructuredLLMClientProtocol,
    *,
    model_name: str,
    summarizer: Summarizer | None = None,
) -> SqlAlchemyTurnOrchestrator:
    """Construct SQLAlchemy repositories with real structured Agent classes."""

    return SqlAlchemyTurnOrchestrator(
        session_factory=session_factory,
        task_queue_factory=_post_turn_task_queue_factory(
            summarizer or RollingSummarizer(structured_client, model_name=model_name)
        ),
        risk_agent=RiskAgent(structured_client, model_name=model_name),
        state_tracker=StateTracker(structured_client, model_name=model_name),
        strategy_planner=StrategyPlanner(structured_client, model_name=model_name),
        feedback_evaluator=FeedbackEvaluator(
            structured_client,
            model_name=model_name,
        ),
        response_agent=FakeResponseAgent(),
        output_guard=FakeOutputGuard(),
    )


def build_sqlalchemy_session_closer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    summarizer: Summarizer | None = None,
    finalizer: SessionFinalizerProtocol | None = None,
    memory_curator: MemoryCuratorProtocol | None = None,
    memory_policy: MemoryPolicy | None = None,
    ensure_summary: bool = True,
) -> SqlAlchemySessionCloser:
    """Build the transactional SQLAlchemy session-close and memory runtime."""

    return SqlAlchemySessionCloser(
        session_factory=session_factory,
        summarizer=summarizer or FakeRollingSummarizer(),
        finalizer=finalizer or FakeSessionFinalizer(),
        memory_curator=memory_curator or MemoryCurator(),
        memory_policy=memory_policy or MemoryPolicy(),
        ensure_summary=ensure_summary,
    )


def _post_turn_task_queue_factory(summarizer: Summarizer) -> TaskQueueFactory:
    """Create transaction-local post-turn workers so they can observe turn writes."""

    def task_queue_factory(session: AsyncSession) -> TaskQueue:
        message_repository = SqlAlchemyMessageRepository(session)
        state_repository = SqlAlchemyStateRepository(session)
        summary_repository = SqlAlchemySummaryRepository(session)
        intervention_repository = SqlAlchemyInterventionRepository(session)
        summary_worker = SummaryWorker(
            message_repository=message_repository,
            state_repository=state_repository,
            summary_repository=summary_repository,
            intervention_repository=intervention_repository,
            summarizer=summarizer,
        )
        return InlinePostTurnTaskQueue(PostTurnWorker(summary_worker))

    return task_queue_factory


async def build_application_runtime(
    settings: Settings | None = None,
    *,
    engine_factory: EngineFactory = create_engine,
    http_client_factory: HTTPClientFactory | None = None,
    redis_client_factory: RedisClientFactory | None = None,
    structured_client_wrapper: StructuredClientWrapper | None = None,
) -> ApplicationRuntime:
    """Build the selected process runtime without fallback or schema creation."""

    active_settings = settings or get_settings()
    mode = active_settings.app_runtime_mode
    if mode not in _RUNTIME_MODES:
        raise RuntimeConfigurationError("APP_RUNTIME_MODE is invalid")
    if active_settings.app_env.casefold() == "production" and mode == "in_memory":
        raise RuntimeConfigurationError("production cannot use the default in_memory runtime")
    redis_client: RedisResource | None = None
    engine: AsyncEngine | None = None
    http_client: OpenAICompatibleHTTPClient | None = None
    try:
        redis_client = create_redis_client(
            active_settings,
            client_factory=redis_client_factory,
        )
        if (
            redis_client is not None
            and active_settings.redis_required
            and active_settings.app_env.casefold() == "production"
        ):
            await redis_client.ping(timeout_seconds=active_settings.health_check_timeout_seconds)
        if mode == "in_memory":
            return ApplicationRuntime(
                settings=active_settings,
                mode=mode,
                orchestrator=build_in_memory_orchestrator(),
                redis_client=redis_client,
                redis_required=active_settings.redis_required,
            )
        try:
            engine = engine_factory(active_settings)
        except (ArgumentError, ValueError) as exc:
            raise RuntimeConfigurationError("DATABASE_URL is invalid") from exc
        session_factory = create_session_factory(engine)

        if mode == "sqlalchemy_fake":
            orchestrator = build_sqlalchemy_orchestrator_with_post_turn_summary(session_factory)
            session_closer = build_sqlalchemy_session_closer(session_factory)
            return _sqlalchemy_runtime(
                active_settings,
                mode,
                engine,
                session_factory,
                orchestrator,
                redis_client=redis_client,
                session_closer=session_closer,
            )

        client_factory = http_client_factory or _build_http_client
        http_client = client_factory(active_settings)
        structured_client = StructuredLLMClient(
            http_client,
            default_model_name=active_settings.structured_model_name,
        )
        agent_client = (
            structured_client_wrapper(structured_client, http_client)
            if structured_client_wrapper is not None
            else structured_client
        )
        summarizer = RollingSummarizer(
            agent_client,
            model_name=active_settings.structured_model_name,
        )
        orchestrator = build_model_assisted_sqlalchemy_orchestrator(
            session_factory,
            agent_client,
            model_name=active_settings.structured_model_name,
            summarizer=summarizer,
        )
        session_closer = build_sqlalchemy_session_closer(
            session_factory,
            summarizer=summarizer,
            finalizer=SessionFinalizer(
                agent_client,
                model_name=active_settings.structured_model_name,
            ),
            memory_curator=MemoryCurator(
                agent_client,
                model_name=active_settings.structured_model_name,
            ),
        )
        return _sqlalchemy_runtime(
            active_settings,
            mode,
            engine,
            session_factory,
            orchestrator,
            http_client=http_client,
            structured_client=structured_client,
            agent_structured_client=agent_client,
            redis_client=redis_client,
            session_closer=session_closer,
        )
    except BaseException:
        try:
            if redis_client is not None:
                await redis_client.aclose()
        finally:
            try:
                if http_client is not None:
                    await http_client.aclose()
            finally:
                if engine is not None:
                    await engine.dispose()
        raise


def _sqlalchemy_runtime(
    settings: Settings,
    mode: AppRuntimeMode,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    orchestrator: SqlAlchemyTurnOrchestrator,
    *,
    redis_client: RedisResource | None = None,
    http_client: OpenAICompatibleHTTPClient | None = None,
    structured_client: StructuredLLMClient | None = None,
    agent_structured_client: StructuredLLMClientProtocol | None = None,
    session_closer: SqlAlchemySessionCloser | None = None,
) -> ApplicationRuntime:
    """Collect one fully assembled SQLAlchemy runtime resource set."""

    return ApplicationRuntime(
        settings=settings,
        mode=mode,
        orchestrator=orchestrator,
        redis_client=redis_client,
        redis_required=settings.redis_required,
        engine=engine,
        session_factory=session_factory,
        http_client=http_client,
        structured_client=structured_client,
        agent_structured_client=agent_structured_client,
        risk_agent=orchestrator.risk_agent,
        state_tracker=orchestrator.state_tracker,
        strategy_planner=orchestrator.strategy_planner,
        response_agent=orchestrator.response_agent,
        output_guard=orchestrator.output_guard,
        feedback_evaluator=orchestrator.feedback_evaluator,
        task_queue=orchestrator.task_queue,
        session_closer=session_closer,
    )


def _build_http_client(settings: Settings) -> OpenAICompatibleHTTPClient:
    """Build the formal provider-agnostic client from shared settings."""

    return OpenAICompatibleHTTPClient.from_settings(settings)


def get_shared_orchestrator() -> TurnOrchestrator:
    """Return the legacy in-memory singleton used by direct local tests."""

    global _shared_orchestrator
    if _shared_orchestrator is None:
        _shared_orchestrator = build_in_memory_orchestrator()
    return _shared_orchestrator


def reset_shared_orchestrator() -> None:
    """Reset the legacy process-local orchestrator singleton for tests."""

    global _shared_orchestrator
    _shared_orchestrator = None


__all__ = [
    "build_application_runtime",
    "build_in_memory_orchestrator",
    "build_model_assisted_in_memory_orchestrator",
    "build_model_assisted_sqlalchemy_orchestrator",
    "build_sqlalchemy_orchestrator",
    "build_sqlalchemy_orchestrator_with_post_turn_summary",
    "build_sqlalchemy_session_closer",
    "get_shared_orchestrator",
    "reset_shared_orchestrator",
]
