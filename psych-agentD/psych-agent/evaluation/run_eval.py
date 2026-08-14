"""Offline deterministic loop evaluation CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from agents.feedback_evaluator import FakeFeedbackEvaluator
from agents.memory_curator import FakeMemoryCurator, MemoryCuratorInput
from agents.output_guard import FakeOutputGuard
from agents.response_agent import FakeResponseAgent
from agents.risk_agent import FakeRiskAgent
from agents.rolling_summarizer import FakeRollingSummarizer
from agents.state_tracker import FakeStateTracker
from agents.strategy_planner import FakeStrategyPlanner
from evaluation.evaluators.memory_evaluator import (
    ExpectedMemoryCandidate,
    ExpectedMemoryDecision,
    evaluate_memory_candidate,
    evaluate_memory_policy,
)
from evaluation.evaluators.safety_evaluator import ExpectedRisk, evaluate_safety
from evaluation.evaluators.state_evaluator import evaluate_state
from evaluation.evaluators.strategy_evaluator import (
    ExpectedFeedback,
    ExpectedStrategy,
    evaluate_strategy,
)
from evaluation.evaluators.summary_evaluator import evaluate_summary_faithfulness
from evaluation.results import EvaluationCaseResult, EvaluationReport, stable_reason_codes
from orchestrator.post_turn_pipeline import NoopTaskQueue
from orchestrator.turn_orchestrator import TurnOrchestrator
from schemas.common import (
    InterventionId,
    MemoryId,
    MessageId,
    SessionId,
    SourceReference,
    UserId,
)
from schemas.feedback import FeedbackLabel, FeedbackResult, ObjectiveProgress, StrategyFit
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.memory import MemoryCandidate, MemoryPolicyInput, RetrievedMemories
from schemas.messages import Message, MessageRole
from schemas.risk import RiskInput, RiskLevel, RiskResult, RiskRoute
from schemas.state import SessionState, StateItem
from schemas.strategy import StrategyPlan, StrategyPlannerInput
from schemas.summary import RollingSummarizerInput, SessionFinalizerResult
from services.context_builder import ContextBuilder
from services.memory_policy import MemoryPolicy
from services.memory_retriever import MemoryRetriever
from services.state_reducer import StateReducer
from storage.repositories.intervention_repository import InMemoryInterventionRepository
from storage.repositories.message_repository import InMemoryMessageRepository
from storage.repositories.state_repository import InMemoryStateRepository
from storage.repositories.summary_repository import InMemorySummaryRepository

_DATASET_DIR = Path(__file__).resolve().parent / "datasets"


class EvaluationConfigurationError(ValueError):
    """Raised for invalid datasets or runner configuration."""


async def run_evaluation(dataset_dir: Path | None = None) -> EvaluationReport:
    """Run every configured case while preserving suite and case order."""

    root = dataset_dir or _DATASET_DIR
    results: list[EvaluationCaseResult] = []
    for suite, filename, runner in (
        ("adaptive_loop", "adaptive_loop_cases.json", _run_adaptive_case),
        ("summary", "summary_cases.json", _run_summary_case),
        ("memory", "memory_cases.json", _run_memory_case),
        ("safety", "safety_cases.json", _run_safety_case),
    ):
        for case in _load_cases(root / filename):
            case_id = _string(case, "id")
            try:
                reasons = await runner(case)
            except EvaluationConfigurationError:
                raise
            except (ValidationError, ValueError) as error:
                raise EvaluationConfigurationError(
                    f"invalid typed input for {suite}:{case_id}: {error}"
                ) from error
            except Exception:
                reasons = ("case_execution_error",)
            results.append(
                EvaluationCaseResult(
                    suite=suite,
                    case_id=case_id,
                    passed=not reasons,
                    reason_codes=reasons,
                )
            )
    return EvaluationReport(tuple(results))


async def _run_adaptive_case(case: dict[str, object]) -> tuple[str, ...]:
    input_data = _dict(case, "input")
    expected = _dict(case, "expected")
    user_id = UserId(_string(input_data, "user_id"))
    session_id = SessionId(_string(input_data, "session_id"))
    turns = _strings(input_data, "user_turns")
    orchestrator, messages, interventions, states = _build_orchestrator()
    for turn in turns:
        await orchestrator.handle_turn(user_id=user_id, session_id=session_id, text=turn)

    reasons: list[str] = []
    records = await interventions.list_for_session(session_id)
    feedback: FeedbackResult | None = None
    plan: StrategyPlan | None = None
    feedback_data = _optional_dict(expected, "feedback")
    if feedback_data is not None:
        evaluated = next((item for item in records if item.explicit_feedback is not None), None)
        if (
            evaluated is not None
            and evaluated.explicit_feedback is not None
            and evaluated.objective_progress is not None
            and evaluated.strategy_fit is not None
        ):
            feedback = FeedbackResult(
                observed_response=evaluated.observed_response or "",
                explicit_feedback=FeedbackLabel(evaluated.explicit_feedback),
                objective_progress=ObjectiveProgress(evaluated.objective_progress),
                strategy_fit=StrategyFit(evaluated.strategy_fit),
                recommended_adjustment=evaluated.recommended_adjustment,
                confidence=1.0,
            )
        pending = records[-1] if records else None
        if pending is not None:
            state = await states.get_current(session_id)
            if state is not None:
                plan = await FakeStrategyPlanner().plan(_strategy_input(state, feedback))
        reasons.extend(
            evaluate_strategy(
                feedback,
                plan,
                expected_feedback=_expected_feedback(feedback_data),
                expected_strategy=ExpectedStrategy(
                    next_strategy_one_of=tuple(_strings(expected, "next_strategy_one_of")),
                    rejected_strategy=_optional_string(expected, "rejected_strategy"),
                    required_avoid=tuple(_optional_strings(expected, "required_avoid")),
                ),
            )
        )
    state_data = _optional_dict(expected, "state_delta")
    if state_data is not None:
        state = await states.get_current(session_id)
        if state is None:
            reasons.append("state_missing")
        else:
            input_messages = await messages.get_recent(session_id, limit=100)
            reasons.extend(
                evaluate_state(
                    state,
                    strategy_preferences_contains=_optional_string(
                        state_data, "strategy_preferences_contains"
                    ),
                    input_message_ids={message.id for message in input_messages},
                )
            )
    return stable_reason_codes(reasons)


def _strategy_input(state: SessionState, feedback: FeedbackResult | None) -> StrategyPlannerInput:
    return StrategyPlannerInput(
        session_state=state,
        risk=RiskResult(
            risk_level=RiskLevel.LOW,
            route=RiskRoute.NORMAL,
            confidence=1.0,
        ),
        feedback=feedback,
        memories=RetrievedMemories(),
    )


async def _run_summary_case(case: dict[str, object]) -> tuple[str, ...]:
    input_data = _dict(case, "input")
    expected = _dict(case, "expected")
    session_id = SessionId(_string(input_data, "session_id"))
    messages = _messages(input_data, session_id)
    state = _session_state(_dict(input_data, "current_state"), session_id)
    interventions = _interventions(input_data, session_id)
    summary = await FakeRollingSummarizer().summarize(
        RollingSummarizerInput(
            session_id=session_id,
            uncovered_messages=messages,
            current_state=state,
            interventions=interventions,
        )
    )
    reasons = list(
        evaluate_summary_faithfulness(summary, messages, interventions=interventions).reason_codes
    )
    if str(summary.covered_from) != _string(expected, "covered_from"):
        reasons.append("summary_covered_from_mismatch")
    if str(summary.covered_to) != _string(expected, "covered_to"):
        reasons.append("summary_covered_to_mismatch")
    for field in ("current_problem", "session_goal"):
        if not _contains_all(
            getattr(summary, field) or "", _strings(expected, f"{field}_contains")
        ):
            reasons.append(f"summary_{field}_mismatch")
    if not all(
        any(fragment in item for item in summary.important_user_statements)
        for fragment in _strings(expected, "important_user_statements_contains")
    ):
        reasons.append("summary_user_statement_missing")
    if not all(
        item in summary.strategies_attempted
        for item in _strings(expected, "strategies_attempted_contains")
    ):
        reasons.append("summary_strategy_missing")
    if not set(_strings(expected, "source_message_ids_must_include")).issubset(
        map(str, summary.source_message_ids)
    ):
        reasons.append("summary_source_missing")
    return stable_reason_codes(reasons)


async def _run_memory_case(case: dict[str, object]) -> tuple[str, ...]:
    kind = _string(case, "kind")
    input_data = _dict(case, "input")
    expected = _dict(case, "expected")
    session_id = SessionId(_string(input_data, "session_id"))
    if kind == "curator":
        messages = _messages(input_data, session_id)
        final_state = SessionState(session_id=session_id)
        finalizer = SessionFinalizerResult(
            session_id=session_id,
            session_summary="No precomputed candidates.",
            source_message_ids=[message.id for message in messages],
        )
        candidates = await FakeMemoryCurator().curate(
            MemoryCuratorInput(
                session_id=session_id,
                messages=messages,
                final_state=final_state,
                finalizer_result=finalizer,
            )
        )
        
        candidate_expected = _expected_candidate(_dict(expected, "candidate"))
        candidate = next(
            (
                item
                for item in candidates
                if item.candidate_type.value == candidate_expected.candidate_type
            ),
            None,
        )
        return evaluate_memory_candidate(
            candidate,
            candidate_expected,
            input_message_ids={message.id for message in messages},
        )
    if kind == "policy":
        candidate = MemoryCandidate.model_validate(_dict(input_data, "candidate"))
        decision = MemoryPolicy().evaluate_candidate(
            MemoryPolicyInput(
                candidate=candidate,
                user_memory_enabled=_boolean(input_data, "user_memory_enabled"),
                session_id=session_id,
            )
        )
        return evaluate_memory_policy(decision, _expected_decision(_dict(expected, "decision")))
    raise EvaluationConfigurationError(f"unknown memory case kind: {kind}")


async def _run_safety_case(case: dict[str, object]) -> tuple[str, ...]:
    input_data = _dict(case, "input")
    current_message = Message.model_validate(_dict(input_data, "current_message"))
    recent_messages = [
        Message.model_validate(_as_dict(item, "recent_message"))
        for item in _list(input_data, "recent_messages")
    ]
    payload = RiskInput(
        current_message=current_message,
        recent_messages=recent_messages,
        current_risk_level=RiskLevel(_string(input_data, "current_risk_level")),
    )
    actual = await FakeRiskAgent().analyze(payload)
    return evaluate_safety(actual, _expected_risk(_dict(case, "expected_risk")))


def _build_orchestrator() -> tuple[
    TurnOrchestrator,
    InMemoryMessageRepository,
    InMemoryInterventionRepository,
    InMemoryStateRepository,
]:
    messages = InMemoryMessageRepository()
    interventions = InMemoryInterventionRepository()
    states = InMemoryStateRepository()
    orchestrator = TurnOrchestrator(
        risk_agent=FakeRiskAgent(),
        state_tracker=FakeStateTracker(),
        feedback_evaluator=FakeFeedbackEvaluator(),
        strategy_planner=FakeStrategyPlanner(),
        response_agent=FakeResponseAgent(),
        output_guard=FakeOutputGuard(),
        state_reducer=StateReducer(),
        context_builder=ContextBuilder(),
        memory_retriever=MemoryRetriever(),
        message_repository=messages,
        state_repository=states,
        summary_repository=InMemorySummaryRepository(),
        intervention_repository=interventions,
        task_queue=NoopTaskQueue(),
    )
    return orchestrator, messages, interventions, states


def _load_cases(path: Path) -> list[dict[str, object]]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationConfigurationError(str(error)) from error
    root = _as_dict(raw, str(path))
    return [_as_dict(item, f"{path}:case") for item in _list(root, "cases")]


def _messages(data: dict[str, object], session_id: SessionId) -> list[Message]:
    result: list[Message] = []
    for index, raw in enumerate(_list(data, "messages"), start=1):
        item = _as_dict(raw, "message")
        result.append(
            Message(
                id=MessageId(_string(item, "id")),
                session_id=session_id,
                role=MessageRole(_string(item, "role")),
                content=_string(item, "content"),
                sequence_number=index,
            )
        )
    return result


def _session_state(data: dict[str, object], session_id: SessionId) -> SessionState:
    def items(key: str) -> list[StateItem]:
        return [
            StateItem(
                value=_string(item, "value"),
                source=SourceReference(message_id=MessageId(_string(item, "source_message_id"))),
            )
            for item in (_as_dict(raw, key) for raw in _optional_list(data, key))
        ]

    return SessionState(
        session_id=session_id,
        session_goal=_optional_string(data, "session_goal"),
        active_topics=items("active_topics"),
        reported_emotions=items("reported_emotions"),
        user_preferences=items("user_preferences"),
    )


def _interventions(data: dict[str, object], session_id: SessionId) -> list[InterventionRecord]:
    result: list[InterventionRecord] = []
    for raw in _optional_list(data, "interventions"):
        item = _as_dict(raw, "intervention")
        result.append(
            InterventionRecord(
                intervention_id=InterventionId(_string(item, "id")),
                session_id=session_id,
                assistant_message_id=MessageId(_string(item, "assistant_message_id")),
                strategy=_string(item, "strategy"),
                objective=_string(item, "objective"),
                status=InterventionStatus(_string(item, "status")),
            )
        )
    return result


def _expected_feedback(data: dict[str, object]) -> ExpectedFeedback:
    return ExpectedFeedback(
        _string(data, "explicit_feedback"),
        _string(data, "strategy_fit"),
        _string(data, "objective_progress"),
        _optional_string(data, "recommended_adjustment"),
    )


def _expected_candidate(data: dict[str, object]) -> ExpectedMemoryCandidate:
    return ExpectedMemoryCandidate(
        _string(data, "candidate_type"),
        tuple(_strings(data, "content_contains")),
        tuple(_strings(data, "source_message_ids")),
        _string(data, "source_type"),
        _string(data, "sensitivity"),
        _string(data, "recommended_operation"),
        _boolean(data, "requires_user_confirmation"),
    )


def _expected_decision(data: dict[str, object]) -> ExpectedMemoryDecision:
    target = _optional_string(data, "target_memory_id")
    return ExpectedMemoryDecision(
        _boolean(data, "allowed"),
        _optional_string(data, "operation"),
        _boolean(data, "requires_user_confirmation"),
        tuple(_optional_strings(data, "reason_codes")),
        MemoryId(target) if target else None,
        target is not None,
    )


def _expected_risk(data: dict[str, object]) -> ExpectedRisk:
    return ExpectedRisk(
        risk_level_one_of=tuple(_optional_strings(data, "risk_level_one_of")),
        route_one_of=tuple(_optional_strings(data, "route_one_of")),
        categories_contains=tuple(_optional_strings(data, "categories_contains")),
        categories_exact=(
            tuple(_strings(data, "categories_exact")) if "categories_exact" in data else None
        ),
        categories_excludes=tuple(_optional_strings(data, "categories_excludes")),
        needs_clarification=_optional_boolean(data, "needs_clarification"),
        reason_codes_contains=tuple(_optional_strings(data, "reason_codes_contains")),
        minimum_confidence=_optional_float(data, "minimum_confidence"),
    )


def format_report(report: EvaluationReport) -> str:
    lines = [
        "Loop evaluation report",
        f"total: {report.total}",
        f"passed: {report.passed}",
        f"failed: {report.failed}",
    ]
    for result in report.case_results:
        lines.append(f"[{'PASS' if result.passed else 'FAIL'}] {result.suite}:{result.case_id}")
        if not result.passed:
            lines.extend(f"  - {reason}" for reason in result.reason_codes)
    return "\n".join(lines)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_evaluation(args.dataset_dir))
    except EvaluationConfigurationError as error:
        print(f"Loop evaluation configuration error: {error}")
        return 2
    print(format_report(report))
    return 0 if report.is_success else 1


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise EvaluationConfigurationError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _dict(data: dict[str, object], key: str) -> dict[str, object]:
    return _as_dict(_required(data, key), key)


def _optional_dict(data: dict[str, object], key: str) -> dict[str, object] | None:
    return None if key not in data else _as_dict(data[key], key)


def _list(data: dict[str, object], key: str) -> list[object]:
    value = _required(data, key)
    if not isinstance(value, list):
        raise EvaluationConfigurationError(f"{key} must be a list")
    return cast(list[object], value)


def _optional_list(data: dict[str, object], key: str) -> list[object]:
    return [] if key not in data else _list(data, key)


def _string(data: dict[str, object], key: str) -> str:
    value = _required(data, key)
    if not isinstance(value, str):
        raise EvaluationConfigurationError(f"{key} must be a string")
    return value


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvaluationConfigurationError(f"{key} must be a string or null")
    return value


def _strings(data: dict[str, object], key: str) -> list[str]:
    return [_expect_string(item, key) for item in _list(data, key)]


def _optional_strings(data: dict[str, object], key: str) -> list[str]:
    return [] if key not in data else _strings(data, key)


def _boolean(data: dict[str, object], key: str) -> bool:
    value = _required(data, key)
    if not isinstance(value, bool):
        raise EvaluationConfigurationError(f"{key} must be boolean")
    return value


def _optional_boolean(data: dict[str, object], key: str) -> bool | None:
    if key not in data:
        return None
    return _boolean(data, key)


def _optional_float(data: dict[str, object], key: str) -> float | None:
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationConfigurationError(f"{key} must be a number")
    return float(value)


def _required(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise EvaluationConfigurationError(f"missing field: {key}")
    return data[key]


def _expect_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EvaluationConfigurationError(f"{label} item must be string")
    return value


def _contains_all(text: str, fragments: list[str]) -> bool:
    return all(fragment in text for fragment in fragments)


if __name__ == "__main__":
    raise SystemExit(cli())
