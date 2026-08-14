"""Reusable intervention fixtures."""

from schemas.common import InterventionId, MessageId, SessionId
from schemas.intervention import InterventionRecord, InterventionStatus


def pending_reflective_intervention() -> InterventionRecord:
    """Return a pending reflective-listening intervention."""

    return InterventionRecord(
        intervention_id=InterventionId("int_001"),
        session_id=SessionId("session_fixture_work_stress"),
        assistant_message_id=MessageId("msg_002"),
        strategy="reflective_listening",
        objective="帮助用户表达工作沟通带来的压力",
        expected_signals=["用户愿意补充更多情境", "用户表达被理解"],
        status=InterventionStatus.PENDING,
    )


def evaluated_poor_fit_intervention() -> InterventionRecord:
    """Return an evaluated intervention with poor strategy fit."""

    return InterventionRecord(
        intervention_id=InterventionId("int_001"),
        session_id=SessionId("session_fixture_work_stress"),
        assistant_message_id=MessageId("msg_002"),
        strategy="reflective_listening",
        objective="帮助用户表达工作沟通带来的压力",
        expected_signals=["用户愿意补充更多情境", "用户表达被理解"],
        status=InterventionStatus.EVALUATED,
        observed_response="用户拒绝继续情绪分析，并要求具体行动建议。",
        explicit_feedback="negative",
        strategy_fit="poor",
        objective_progress="not_achieved",
        recommended_adjustment="switch_to_collaborative_problem_solving",
    )
