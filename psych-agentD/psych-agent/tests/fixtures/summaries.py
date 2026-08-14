"""Reusable summary and finalizer fixtures."""

from schemas.common import MessageId, SessionId
from schemas.summary import (
    ActionItem,
    RollingSummarizerInput,
    RollingSummary,
    SessionFinalizerInput,
    SessionFinalizerResult,
    StrategyOutcome,
)
from tests.fixtures.interventions import evaluated_poor_fit_intervention
from tests.fixtures.memories import interaction_preference_candidate
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import work_stress_state


def work_stress_rolling_summary() -> RollingSummary:
    """Return a rolling summary for the work-stress fixture dialogue."""

    return RollingSummary(
        session_id=SessionId("session_fixture_work_stress"),
        summary_version=1,
        covered_from=MessageId("msg_001"),
        covered_to=MessageId("msg_003"),
        current_problem="用户和直属领导沟通紧张，开会前焦虑。",
        session_goal="找到一个低压力沟通步骤。",
        important_user_statements=[
            "用户不想继续分析情绪。",
            "用户想知道下一步具体怎么做。",
        ],
        strategies_attempted=["reflective_listening"],
        strategy_responses=["用户明确要求切换到具体行动建议。"],
        open_questions=["用户愿意尝试哪一种沟通开场方式？"],
        source_message_ids=[MessageId("msg_001"), MessageId("msg_003")],
    )


def rolling_summarizer_input() -> RollingSummarizerInput:
    """Return input for RollingSummarizer tests."""

    return RollingSummarizerInput(
        session_id=SessionId("session_fixture_work_stress"),
        previous_summary=None,
        uncovered_messages=work_stress_dialogue(),
        current_state=work_stress_state(),
        interventions=[evaluated_poor_fit_intervention()],
    )


def session_finalizer_input() -> SessionFinalizerInput:
    """Return input for SessionFinalizer tests."""

    return SessionFinalizerInput(
        session_id=SessionId("session_fixture_work_stress"),
        messages=work_stress_dialogue(),
        final_state=work_stress_state(),
        interventions=[evaluated_poor_fit_intervention()],
        rolling_summary=work_stress_rolling_summary(),
    )


def session_finalizer_result() -> SessionFinalizerResult:
    """Return an expected SessionFinalizerResult fixture."""

    return SessionFinalizerResult(
        session_id=SessionId("session_fixture_work_stress"),
        session_summary="用户讨论了和直属领导沟通紧张的问题，并希望获得低压力小步骤。",
        goal_updates=["继续探索和直属领导沟通的低压力步骤"],
        unfinished_topics=["如何开口和直属领导约一次短沟通"],
        action_items=[ActionItem(content="写下一句想对直属领导说的开场白")],
        candidate_memories=[interaction_preference_candidate()],
        strategy_outcomes=[
            StrategyOutcome(
                strategy="reflective_listening",
                outcome="poor_fit_after_user_requested_concrete_steps",
                source_message_ids=[MessageId("msg_003")],
            )
        ],
        risk_events=[],
        source_message_ids=[MessageId("msg_001"), MessageId("msg_003"), MessageId("msg_005")],
    )
