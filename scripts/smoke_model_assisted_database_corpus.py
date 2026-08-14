"""Run the model-assisted database corpus against a selected database backend."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


class ScriptedStructuredModelClient:
    """Base LLM client that returns structured JSON based on the requested agent."""

    def __init__(self) -> None:
        """Initialize request accounting for the smoke output."""

        self.requests: list[Any] = []
        self._strategy_calls = 0
        self._state_calls = 0

    async def generate(self, request: Any) -> Any:
        """Return a normal LLMResponse containing scripted JSON text."""

        from schemas.llm import LLMResponse

        self.requests.append(request)
        prompt = str(request.messages[0].content)
        if "agent: risk_agent" in prompt:
            content = self._risk_json()
        elif "agent: state_tracker" in prompt:
            content = self._state_delta_json(prompt)
        elif "agent: strategy_planner" in prompt:
            content = self._strategy_json(prompt)
        else:
            msg = "unexpected structured model request"
            raise RuntimeError(msg)
        return LLMResponse(
            content=content,
            model_name=request.model_name,
            finish_reason="stop",
        )

    def _risk_json(self) -> str:
        """Return low-risk JSON for the work-stress fixture turns."""

        return json.dumps(
            {
                "risk_level": "low",
                "categories": [],
                "needs_clarification": False,
                "route": "normal_dialogue",
                "reason_codes": ["fixture_low"],
                "confidence": 0.91,
            }
        )

    def _state_delta_json(self, prompt: str) -> str:
        """Return StateDelta JSON using the actual current message ID from the prompt."""

        self._state_calls += 1
        message_id = _extract_current_message_id(prompt)
        text = _extract_current_message_text(prompt)
        preferences: list[dict[str, str]] = []
        if self._state_calls >= 2:
            preferences.append(
                {
                    "operation": "add",
                    "value": "prefers concrete low-pressure steps",
                    "source_message_id": message_id,
                }
            )
        if self._state_calls >= 3:
            preferences.append(
                {
                    "operation": "add",
                    "value": "prefers one small step at a time",
                    "source_message_id": message_id,
                }
            )
        return json.dumps(
            {
                "explicit_user_request": text,
                "topic_updates": [
                    {
                        "operation": "add",
                        "topic": "work communication stress",
                        "source_message_id": message_id,
                    }
                ],
                "goal_updates": [
                    {
                        "operation": "update",
                        "goal": "support work communication planning",
                        "source_message_id": message_id,
                    }
                ],
                "reported_emotions": [],
                "user_corrections": [],
                "strategy_preferences": preferences,
                "hypotheses": [],
            }
        )

    def _strategy_json(self, prompt: str) -> str:
        """Return a strategy JSON that changes after preferences appear."""

        self._strategy_calls += 1
        wants_concrete = "prefers concrete low-pressure steps" in prompt
        wants_one_step = "prefers one small step at a time" in prompt
        strategy = "reflective_listening"
        phase = "exploration"
        if self._strategy_calls > 1 or wants_concrete or wants_one_step:
            strategy = "collaborative_problem_solving"
            phase = "intervention"
        return json.dumps(
            {
                "conversation_phase": phase,
                "primary_strategy": strategy,
                "objective": "keep the support focused and concrete",
                "reason": "scripted fixture strategy for DB smoke test",
                "avoid": ["diagnosis", "medication advice"],
                "expected_signals": ["user can continue the conversation"],
                "switch_conditions": ["risk escalates", "strategy does not fit"],
            }
        )


def _extract_current_message_id(prompt: str) -> str:
    """Extract the current message ID from a StateTracker prompt."""

    match = re.search(r"current_message:\s*\nid: (?P<message_id>\S+)", prompt)
    if match is None:
        return "msg_unknown"
    return match.group("message_id")


def _extract_current_message_text(prompt: str) -> str:
    """Extract the current user message text from a StateTracker prompt."""

    match = re.search(r"content: (?P<content>.*?)\n\nrecent_messages:", prompt, re.DOTALL)
    if match is None:
        return ""
    return match.group("content").strip()


async def run_business_flow(
    session_factory: async_sessionmaker[AsyncSession],
    case: SmokeCase,
) -> BusinessOutcome:
    """Run three turns and one summary through the existing repositories."""

    from agents.feedback_evaluator import FakeFeedbackEvaluator
    from agents.output_guard import FakeOutputGuard
    from agents.response_agent import FakeResponseAgent
    from agents.risk_agent import RiskAgent
    from agents.rolling_summarizer import FakeRollingSummarizer
    from agents.state_tracker import StateTracker
    from agents.strategy_planner import StrategyPlanner
    from llm.structured_client import StructuredLLMClient
    from orchestrator.post_turn_pipeline import NoopTaskQueue
    from orchestrator.turn_orchestrator import TurnOrchestrator
    from schemas.messages import MessageRole
    from schemas.summary import RollingSummarizerInput
    from services.context_builder import ContextBuilder
    from services.memory_retriever import MemoryRetriever
    from services.state_reducer import StateReducer
    from storage.database import (
        transactional_session,
    )
    from storage.repositories.intervention_repository import SqlAlchemyInterventionRepository
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
        message for message in work_stress_dialogue() if message.role == MessageRole.USER
    ]
    for message in user_messages:
        async with transactional_session(session_factory) as session:
            await SqlAlchemyUserRepository(session).ensure_user(user_id)
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
        recent_messages = await message_repository.get_recent(
            user_id, session_id, limit=20
        )
        current_state = await state_repository.get_current(user_id, session_id)
        previous_summary = await summary_repository.get_current(user_id, session_id)
        interventions = await intervention_repository.list_for_session(
            user_id, session_id
        )
        summary = await FakeRollingSummarizer().summarize(
            RollingSummarizerInput(
                session_id=session_id,
                previous_summary=previous_summary,
                uncovered_messages=recent_messages,
                current_state=current_state,
                interventions=interventions,
            )
        )
        await summary_repository.save_version(user_id, summary)

    return BusinessOutcome(
        turns=len(user_messages),
        structured_llm_calls=len(model_client.requests),
        summary_version=summary.summary_version,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the configured smoke and return its stable process exit code."""

    parser = build_parser(__doc__ or "Database corpus smoke")
    options = parse_options(parser, argv)
    result, exit_code = asyncio.run(
        run_database_smoke(
            smoke_name="model_assisted_database_corpus",
            options=options,
            expected=ExpectedSmokeState(
                session_status="active",
                long_term_memories_rows=0,
            ),
            business_flow=run_business_flow,
        )
    )
    print(render_result(result, json_output=options.json_output))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
