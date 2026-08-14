"""Run the close-session memory flow against a selected database backend."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.database_smoke_support import (
    BusinessOutcome,
    ExpectedSmokeState,
    SmokeCase,
    build_parser,
    parse_options,
    render_result,
    run_database_smoke,
)


async def run_business_flow(
    session_factory: async_sessionmaker[AsyncSession],
    case: SmokeCase,
) -> BusinessOutcome:
    """Run turns, summarize, finalize, write memory, and close the session."""

    from agents.feedback_evaluator import FakeFeedbackEvaluator
    from agents.output_guard import FakeOutputGuard
    from agents.response_agent import FakeResponseAgent
    from agents.risk_agent import RiskAgent
    from agents.rolling_summarizer import FakeRollingSummarizer
    from agents.session_finalizer import FakeSessionFinalizer
    from agents.state_tracker import StateTracker
    from agents.strategy_planner import StrategyPlanner
    from llm.structured_client import StructuredLLMClient
    from orchestrator.post_turn_pipeline import NoopTaskQueue
    from orchestrator.turn_orchestrator import TurnOrchestrator
    from schemas.memory import MemoryPolicyInput
    from schemas.messages import MessageRole
    from schemas.summary import RollingSummarizerInput, SessionFinalizerInput
    from scripts.smoke_model_assisted_database_corpus import (
        ScriptedStructuredModelClient,
    )
    from services.context_builder import ContextBuilder
    from services.memory_policy import MemoryPolicy
    from services.memory_retriever import MemoryRetriever
    from services.state_reducer import StateReducer
    from storage.database import transactional_session
    from storage.models.user import UserModel
    from storage.repositories.intervention_repository import (
        SqlAlchemyInterventionRepository,
    )
    from storage.repositories.memory_repository import SqlAlchemyMemoryRepository
    from storage.repositories.message_repository import SqlAlchemyMessageRepository
    from storage.repositories.session_repository import SqlAlchemySessionRepository
    from storage.repositories.state_repository import SqlAlchemyStateRepository
    from storage.repositories.summary_repository import SqlAlchemySummaryRepository
    from storage.repositories.user_repository import SqlAlchemyUserRepository
    from tests.fixtures.messages import work_stress_dialogue

    model_client = ScriptedStructuredModelClient()
    structured_client = StructuredLLMClient(model_client)
    user_id = case.user_id
    session_id = case.session_id
    user_messages = [
        message
        for message in work_stress_dialogue()
        if message.role == MessageRole.USER
    ]
    for message in user_messages:
        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
            user_row = await session.get(UserModel, str(user_id))
            if user_row is not None:
                user_row.memory_enabled = True
            await SqlAlchemySessionRepository(session).ensure_session(
                user_id, session_id
            )
            orchestrator = TurnOrchestrator(
                risk_agent=RiskAgent(structured_client),
                state_tracker=StateTracker(structured_client),
                feedback_evaluator=FakeFeedbackEvaluator(),
                strategy_planner=StrategyPlanner(structured_client),
                response_agent=FakeResponseAgent(),
                output_guard=FakeOutputGuard(),
                state_reducer=StateReducer(),
                context_builder=ContextBuilder(),
                memory_retriever=MemoryRetriever(),
                message_repository=SqlAlchemyMessageRepository(session),
                state_repository=SqlAlchemyStateRepository(session),
                summary_repository=SqlAlchemySummaryRepository(session),
                intervention_repository=SqlAlchemyInterventionRepository(session),
                task_queue=NoopTaskQueue(),
            )
            await orchestrator.handle_turn(
                user_id=user_id,
                session_id=session_id,
                text=message.content,
            )

    async with transactional_session(session_factory) as session:
        message_repository = SqlAlchemyMessageRepository(session)
        state_repository = SqlAlchemyStateRepository(session)
        summary_repository = SqlAlchemySummaryRepository(session)
        intervention_repository = SqlAlchemyInterventionRepository(session)
        memory_repository = SqlAlchemyMemoryRepository(session)
        session_repository = SqlAlchemySessionRepository(session)

        messages = await message_repository.get_recent(
            user_id, session_id, limit=20
        )
        current_state = await state_repository.get_current(user_id, session_id)
        if current_state is None:
            raise RuntimeError("session state is required before finalizing")
        interventions = await intervention_repository.list_for_session(
            user_id, session_id
        )
        previous_summary = await summary_repository.get_current(user_id, session_id)
        summary = await FakeRollingSummarizer().summarize(
            RollingSummarizerInput(
                session_id=session_id,
                previous_summary=previous_summary,
                uncovered_messages=messages,
                current_state=current_state,
                interventions=interventions,
            )
        )
        await summary_repository.save_version(user_id, summary)
        finalizer_result = await FakeSessionFinalizer().finalize(
            SessionFinalizerInput(
                session_id=session_id,
                messages=messages,
                final_state=current_state,
                interventions=interventions,
                rolling_summary=summary,
            )
        )
        memory_write_count = 0
        policy = MemoryPolicy()
        for candidate in finalizer_result.candidate_memories:
            existing = await memory_repository.list_active(user_id)
            decision = policy.evaluate_candidate(
                MemoryPolicyInput(
                    candidate=candidate,
                    existing_memories=existing,
                    user_memory_enabled=True,
                    session_id=session_id,
                )
            )
            write_result = await memory_repository.create(
                user_id,
                candidate,
                decision,
            )
            if write_result.applied:
                memory_write_count += 1
        await session_repository.close(user_id, session_id)

    return BusinessOutcome(
        turns=len(user_messages),
        structured_llm_calls=len(model_client.requests),
        summary_version=summary.summary_version,
        candidate_memories=len(finalizer_result.candidate_memories),
        memory_writes=memory_write_count,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the configured smoke and return its stable process exit code."""

    parser = build_parser(__doc__ or "Session close memory smoke")
    options = parse_options(parser, argv)
    result, exit_code = asyncio.run(
        run_database_smoke(
            smoke_name="session_close_memory",
            options=options,
            expected=ExpectedSmokeState(
                session_status="closed",
                long_term_memories_rows=1,
            ),
            business_flow=run_business_flow,
        )
    )
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
