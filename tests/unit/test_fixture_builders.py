"""Tests for reusable Python fixture builders."""

from schemas.intervention import InterventionStatus
from schemas.memory import MemoryOperation, MemoryType
from schemas.messages import MessageRole
from tests.fixtures.interventions import (
    evaluated_poor_fit_intervention,
    pending_reflective_intervention,
)
from tests.fixtures.memories import (
    interaction_preference_candidate,
    memory_policy_input,
    retrieved_work_stress_memories,
)
from tests.fixtures.messages import safety_check_dialogue, work_stress_dialogue
from tests.fixtures.states import work_stress_state
from tests.fixtures.summaries import (
    rolling_summarizer_input,
    session_finalizer_input,
    session_finalizer_result,
    work_stress_rolling_summary,
)


def test_work_stress_dialogue_fixture_has_multiple_turns() -> None:
    """The primary fixture should represent a multi-turn dialogue."""

    messages = work_stress_dialogue()

    assert len(messages) == 5
    assert messages[0].role == MessageRole.USER
    assert messages[1].role == MessageRole.ASSISTANT
    assert messages[-1].sequence_number == 5


def test_safety_dialogue_fixture_contains_one_user_message() -> None:
    """The safety fixture should be a minimal risk-related user message."""

    messages = safety_check_dialogue()

    assert len(messages) == 1
    assert messages[0].role == MessageRole.USER


def test_state_and_intervention_fixtures_are_typed() -> None:
    """State and intervention fixtures should match current schemas."""

    state = work_stress_state()
    pending = pending_reflective_intervention()
    evaluated = evaluated_poor_fit_intervention()

    assert state.version == 2
    assert state.active_topics[0].value == "和直属领导沟通紧张"
    assert pending.status == InterventionStatus.PENDING
    assert evaluated.status == InterventionStatus.EVALUATED
    assert evaluated.strategy_fit == "poor"


def test_memory_fixtures_are_typed() -> None:
    """Memory fixtures should use current memory contracts."""

    candidate = interaction_preference_candidate()
    retrieved = retrieved_work_stress_memories()
    policy_input = memory_policy_input()

    assert candidate.candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert candidate.recommended_operation == MemoryOperation.CREATE
    assert retrieved.active_goals == ["改善和直属领导的沟通"]
    assert policy_input.user_memory_enabled is True


def test_summary_fixtures_are_typed() -> None:
    """Summary and finalizer fixtures should use current contracts."""

    summary = work_stress_rolling_summary()
    summarizer_input = rolling_summarizer_input()
    finalizer_input = session_finalizer_input()
    finalizer_result = session_finalizer_result()

    assert summary.summary_version == 1
    assert len(summarizer_input.uncovered_messages) == 5
    assert finalizer_input.rolling_summary is not None
    assert finalizer_result.candidate_memories[0].content
