"""Integration tests for local and API runtime entrypoints."""

from app.dependencies import build_in_memory_orchestrator, get_orchestrator
from runtime import LocalPsychAgent
from runtime.factory import reset_shared_orchestrator


async def test_local_psych_agent_handles_single_message() -> None:
    """The local adapter should run the same turn flow without FastAPI."""

    agent = LocalPsychAgent()

    result = await agent.handle_message(
        user_id="user_local_1",
        session_id="session_local_1",
        text="I feel anxious and want concrete next steps.",
    )

    assert result.status == "ok"
    assert result.session_id == "session_local_1"
    assert result.state_version == 1
    assert result.response


async def test_local_psych_agent_preserves_multi_turn_state() -> None:
    """A single local adapter instance should preserve its in-memory session."""

    agent = LocalPsychAgent()

    first = await agent.handle_message(
        user_id="user_local_2",
        session_id="session_local_2",
        text="I feel stuck at work and want to talk it through.",
    )
    second = await agent.handle_message(
        user_id="user_local_2",
        session_id="session_local_2",
        text="I do not want more reflection, I need concrete next steps.",
    )

    assert first.state_version == 1
    assert second.state_version == 2
    assert "校准" in second.response


def test_api_dependency_uses_shared_runtime_factory() -> None:
    """The optional API dependency should use runtime factory wiring."""

    reset_shared_orchestrator()

    first = get_orchestrator()
    second = get_orchestrator()
    fresh = build_in_memory_orchestrator()

    assert first is second
    assert fresh is not first
