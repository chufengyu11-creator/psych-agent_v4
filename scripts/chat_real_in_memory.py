"""Interactive real-LLM in-memory chat with observable multi-agent stages."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.feedback_evaluator import FeedbackEvaluator
from agents.output_guard import OutputGuard
from agents.response_agent import ResponseAgent
from agents.risk_agent import RiskAgent
from agents.rolling_summarizer import RollingSummarizer
from agents.state_tracker import StateTracker
from agents.strategy_planner import StrategyPlanner
from app.config import Settings
from llm.exceptions import LLMError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.structured_client import StructuredLLMClient
from orchestrator.post_turn_pipeline import InlinePostTurnTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import SessionId, UserId
from schemas.intervention import InterventionRecord
from schemas.messages import Message
from schemas.state import SessionState, StateItem
from schemas.summary import RollingSummary
from scripts.smoke_real_structured_agents import (
    ObservingStructuredClient,
    StructuredCallObservation,
)
from services.context_builder import ContextBuilder
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository
from workers.post_turn_worker import PostTurnWorker
from workers.summary_worker import SummaryWorker


def build_parser() -> argparse.ArgumentParser:
    """Build the manual chat command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="local-user")
    parser.add_argument("--session-id", default="local-session")
    parser.add_argument(
        "--model-name",
        default=None,
        help="Override the structured model name for all model-backed agents.",
    )
    parser.add_argument(
        "--message-limit",
        type=int,
        default=50,
        help="Maximum recent messages considered by the rolling-summary worker.",
    )
    parser.add_argument(
        "--retain-recent",
        type=int,
        default=2,
        help="Recent messages kept out of the rolling summary.",
    )
    parser.add_argument(
        "--show-messages",
        action="store_true",
        help="Print the current in-memory message list after every turn.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.retain_recent < 1:
        parser.error("--retain-recent must be at least 1")
    if args.message_limit <= args.retain_recent:
        parser.error("--message-limit must be greater than --retain-recent")
    return args


async def run_chat(args: argparse.Namespace) -> int:
    """Run one interactive in-memory conversation against the configured LLM."""

    try:
        settings = Settings()
        main_model_name = args.model_name or settings.main_model_name
        structured_model_name = args.model_name or settings.structured_model_name
        safety_model_name = args.model_name or settings.safety_model_name
        http_client = OpenAICompatibleHTTPClient.from_settings(
            settings,
            model_name=structured_model_name,
        )
    except (ValidationError, LLMError) as exc:
        print(f"配置不可用: {type(exc).__name__}", file=sys.stderr)
        return 2

    structured_client = StructuredLLMClient(
        http_client,
        default_model_name=structured_model_name,
    )
    observed_client = ObservingStructuredClient(structured_client, http_client)

    message_repository = InMemoryMessageRepository()
    state_repository = InMemoryStateRepository()
    summary_repository = InMemorySummaryRepository()
    intervention_repository = InMemoryInterventionRepository()
    summary_worker = SummaryWorker(
        message_repository=message_repository,
        state_repository=state_repository,
        summary_repository=summary_repository,
        intervention_repository=intervention_repository,
        summarizer=RollingSummarizer(
            observed_client,
            model_name=structured_model_name,
        ),
        message_limit=args.message_limit,
        retain_recent_messages=args.retain_recent,
    )
    task_queue = InlinePostTurnTaskQueue(PostTurnWorker(summary_worker))
    orchestrator = TurnOrchestrator(
        risk_agent=RiskAgent(observed_client, model_name=safety_model_name),
        state_tracker=StateTracker(
            observed_client,
            model_name=structured_model_name,
        ),
        feedback_evaluator=FeedbackEvaluator(
            observed_client,
            model_name=structured_model_name,
        ),
        strategy_planner=StrategyPlanner(
            observed_client,
            model_name=structured_model_name,
        ),
        response_agent=ResponseAgent(observed_client, model_name=main_model_name),
        output_guard=OutputGuard(observed_client, model_name=safety_model_name),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=MemoryRetriever(),
        message_repository=message_repository,
        state_repository=state_repository,
        summary_repository=summary_repository,
        intervention_repository=intervention_repository,
        task_queue=task_queue,
    )

    user_id = UserId(args.user_id)
    session_id = SessionId(args.session_id)
    observation_cursor = 0

    print("真实模型 in-memory 多轮诊断聊天")
    print(f"user_id={user_id} session_id={session_id}")
    print(
        "models: "
        f"main={main_model_name} "
        f"structured={structured_model_name} "
        f"safety={safety_model_name}"
    )
    print("输入 exit / quit / q 退出。")

    try:
        while True:
            try:
                text = await _read_turn_text()
            except EOFError:
                print()
                break
            text = text.strip()
            if text.casefold() in {"exit", "quit", "q"}:
                break
            if not text:
                continue

            try:
                result = await orchestrator.handle_turn(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                )
            except LLMError as exc:
                print(f"\n模型调用失败: {type(exc).__name__}", file=sys.stderr)
                continue

            observations = observed_client.observations[observation_cursor:]
            observation_cursor = len(observed_client.observations)
            state = await state_repository.get_current(user_id, session_id)
            summary = await summary_repository.get_current(user_id, session_id)
            interventions = await intervention_repository.list_for_session(
                user_id, session_id
            )
            messages = await message_repository.get_recent(
                user_id, session_id, limit=args.message_limit
            )

            print(f"\nAI: {result.response}")
            print(
                f"[status={result.status} state_version={result.state_version} "
                f"message_id={result.message_id}]"
            )
            _print_observations(observations)
            _print_state(state)
            _print_summary(summary)
            _print_interventions(interventions)
            _print_post_turn(task_queue)
            if args.show_messages:
                _print_messages(messages)
    finally:
        print(
            "\n统计: "
            f"api_attempts={http_client.request_count} "
            f"successful_http_responses={http_client.successful_http_response_count}"
        )
        await http_client.aclose()
    return 0


async def _read_turn_text() -> str:
    """Read one line from a terminal or piped stdin."""

    if sys.stdin.isatty():
        return await asyncio.to_thread(input, "\n你: ")
    print("\n你: ", end="", flush=True)
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line


def _print_observations(observations: list[StructuredCallObservation]) -> None:
    """Print safe model-call observations for the latest user turn."""

    print("通路/model calls:")
    if not observations:
        print("  - none")
        return
    for item in observations:
        status = "ok" if item.schema_success else f"fallback_or_error:{item.error_type}"
        print(
            "  - "
            f"{item.agent} model={item.model} "
            f"api={item.api_success} schema={item.schema_success} "
            f"latency_ms={item.latency_ms} status={status}"
        )


def _print_state(state: SessionState) -> None:
    """Print a compact current state snapshot."""

    print("state:")
    print(
        "  "
        f"version={state.version} phase={state.phase.value} "
        f"risk={state.risk_state.level.value} "
        f"risk_categories={state.risk_state.categories}"
    )
    if state.session_goal:
        print(f"  session_goal={_short(state.session_goal)}")
    _print_items("active_topics", state.active_topics)
    _print_items("reported_emotions", state.reported_emotions)
    _print_items("user_preferences", state.user_preferences)
    _print_items("open_questions", state.open_questions)
    if state.pending_action_plan:
        print(f"  pending_action_plan={_short(state.pending_action_plan)}")


def _print_items(label: str, items: list[StateItem]) -> None:
    """Print sourced state item values."""

    if not items:
        return
    values = [f"{_short(item.value, 80)} <- {item.source.message_id}" for item in items]
    print(f"  {label}:")
    for value in values:
        print(f"    - {value}")


def _print_summary(summary: RollingSummary | None) -> None:
    """Print the current rolling summary, if available."""

    print("rolling_summary:")
    if summary is None:
        print("  none yet")
        return
    print(
        "  "
        f"version={summary.summary_version} "
        f"covered={summary.covered_from}->{summary.covered_to}"
    )
    if summary.current_problem:
        print(f"  current_problem={_short(summary.current_problem)}")
    if summary.session_goal:
        print(f"  session_goal={_short(summary.session_goal)}")
    _print_list("important_user_statements", summary.important_user_statements)
    _print_list("strategies_attempted", summary.strategies_attempted)
    _print_list("strategy_responses", summary.strategy_responses)
    _print_list("open_questions", summary.open_questions)
    print(f"  source_message_ids={[str(item) for item in summary.source_message_ids]}")


def _print_interventions(interventions: list[InterventionRecord]) -> None:
    """Print intervention records created or evaluated so far."""

    print("interventions:")
    if not interventions:
        print("  none")
        return
    for item in interventions:
        print(
            "  - "
            f"id={item.intervention_id} status={item.status.value} "
            f"strategy={item.strategy} objective={_short(item.objective, 80)}"
        )
        if item.explicit_feedback is not None:
            print(
                "    "
                f"feedback={item.explicit_feedback} "
                f"fit={item.strategy_fit} progress={item.objective_progress}"
            )


def _print_post_turn(task_queue: InlinePostTurnTaskQueue) -> None:
    """Print the latest inline post-turn worker result."""

    print("post_turn:")
    if not task_queue.results:
        print("  none")
        return
    latest = task_queue.results[-1]
    print(
        "  "
        f"summary_updated={latest.summary_updated} "
        f"summary_version={latest.summary_version}"
    )


def _print_messages(messages: list[Message]) -> None:
    """Print the current in-memory message list."""

    print("messages:")
    for message in messages:
        print(
            "  - "
            f"#{message.sequence_number} {message.role.value} "
            f"{message.id}: {_short(message.content, 120)}"
        )


def _print_list(label: str, values: list[str]) -> None:
    """Print a short list field."""

    if not values:
        return
    print(f"  {label}:")
    for value in values:
        print(f"    - {_short(value, 100)}")


def _short(text: str, limit: int = 140) -> str:
    """Keep terminal diagnostics readable."""

    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def main(argv: Sequence[str] | None = None) -> int:
    """Run the interactive chat."""

    return asyncio.run(run_chat(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
