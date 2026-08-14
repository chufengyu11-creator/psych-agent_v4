"""Integration tests for the initial fake turn pipeline."""

from __future__ import annotations

import pytest

from app.dependencies import build_in_memory_orchestrator
from schemas.common import SessionId, UserId


@pytest.mark.asyncio
async def test_fake_turn_pipeline_returns_typed_response() -> None:
    """The fake pipeline should process a single user turn end to end."""

    orchestrator = build_in_memory_orchestrator()

    result = await orchestrator.handle_turn(
        user_id=UserId("user_1"),
        session_id=SessionId("session_1"),
        text="I feel stuck at work and want to talk it through.",
    )

    assert result.status == "ok"
    assert result.session_id == "session_1"
    assert result.message_id.startswith("msg_")
    assert result.state_version == 1
    assert "最困扰" in result.response


@pytest.mark.asyncio
async def test_fake_turn_pipeline_uses_feedback_to_switch_strategy() -> None:
    """A second turn with explicit rejection should switch the fake strategy."""

    orchestrator = build_in_memory_orchestrator()
    await orchestrator.handle_turn(
        user_id=UserId("user_1"),
        session_id=SessionId("session_1"),
        text="I feel stuck at work and want to talk it through.",
    )

    result = await orchestrator.handle_turn(
        user_id=UserId("user_1"),
        session_id=SessionId("session_1"),
        text="I do not want more reflection, I need concrete next steps.",
    )

    assert result.state_version == 2
    assert "校准" in result.response
