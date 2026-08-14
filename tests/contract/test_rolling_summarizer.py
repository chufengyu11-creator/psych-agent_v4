"""Contract tests for fake and model-backed rolling summarizers."""

from collections.abc import Mapping

import pytest

from agents.rolling_summarizer import FakeRollingSummarizer, RollingSummarizer
from llm.structured_output import ModelT
from schemas.common import MessageId
from schemas.summary import RollingSummarizerInput, RollingSummary
from tests.fixtures.interventions import (
    evaluated_poor_fit_intervention,
    pending_reflective_intervention,
)
from tests.fixtures.messages import work_stress_dialogue
from tests.fixtures.states import empty_state
from tests.fixtures.summaries import rolling_summarizer_input


@pytest.mark.asyncio
async def test_fake_first_summary_has_local_version_coverage_and_sources() -> None:
    """A first fake summary should cover and cite its real input messages."""

    payload = rolling_summarizer_input()
    result = await FakeRollingSummarizer().summarize(payload)

    assert result.summary_version == 1
    assert result.covered_from == payload.uncovered_messages[0].id
    assert result.covered_to == payload.uncovered_messages[-1].id
    assert result.source_message_ids
    assert payload.uncovered_messages[0].id in result.source_message_ids
    assert payload.uncovered_messages[0].content in result.important_user_statements


@pytest.mark.asyncio
async def test_fake_merges_previous_summary_without_duplicates() -> None:
    """A later fake summary should extend coverage while retaining old content."""

    messages = work_stress_dialogue()
    previous = RollingSummary(
        session_id=messages[0].session_id,
        summary_version=1,
        covered_from=messages[0].id,
        covered_to=messages[1].id,
        important_user_statements=[messages[0].content],
        source_message_ids=[messages[0].id],
    )
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        previous_summary=previous,
        uncovered_messages=messages[2:4],
        current_state=empty_state(messages[0].session_id),
    )

    result = await FakeRollingSummarizer().summarize(payload)

    assert result.summary_version == 2
    assert result.covered_from == previous.covered_from
    assert result.covered_to == messages[3].id
    assert result.important_user_statements[0] == messages[0].content
    assert messages[2].content in result.important_user_statements
    assert result.important_user_statements.count(messages[0].content) == 1
    assert result.source_message_ids == [messages[0].id, messages[2].id]


@pytest.mark.asyncio
async def test_fake_returns_previous_summary_when_no_new_messages() -> None:
    """No new coverage must not create a fabricated summary version."""

    messages = work_stress_dialogue()
    previous = RollingSummary(
        session_id=messages[0].session_id,
        summary_version=3,
        covered_from=messages[0].id,
        covered_to=messages[2].id,
        source_message_ids=[messages[0].id, messages[2].id],
    )
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        previous_summary=previous,
        uncovered_messages=[],
        current_state=empty_state(messages[0].session_id),
    )

    assert await FakeRollingSummarizer().summarize(payload) == previous


@pytest.mark.asyncio
async def test_pending_intervention_is_attempted_without_a_response() -> None:
    """A covered pending strategy may be attempted but has no observed outcome."""

    messages = work_stress_dialogue()
    result = await FakeRollingSummarizer().summarize(
        RollingSummarizerInput(
            session_id=messages[0].session_id,
            uncovered_messages=messages[:2],
            current_state=empty_state(messages[0].session_id),
            interventions=[pending_reflective_intervention()],
        )
    )

    assert "reflective_listening" in result.strategies_attempted
    assert result.strategy_responses == []


@pytest.mark.asyncio
async def test_only_evaluated_intervention_produces_strategy_response() -> None:
    """Evaluated feedback should be included while pending feedback stays absent."""

    messages = work_stress_dialogue()
    result = await FakeRollingSummarizer().summarize(
        RollingSummarizerInput(
            session_id=messages[0].session_id,
            uncovered_messages=messages[:3],
            current_state=empty_state(messages[0].session_id),
            interventions=[evaluated_poor_fit_intervention()],
        )
    )

    assert any("negative" in response for response in result.strategy_responses)


class ScriptedClient:
    """Current structured-client test double with configurable output."""

    def __init__(self, result_data: dict[str, object] | None = None) -> None:
        self.result_data = result_data or {
            "session_id": "model-session",
            "summary_version": 99,
            "covered_from": "msg_001",
            "covered_to": "msg_003",
            "current_problem": "model-backed summary",
            "source_message_ids": ["msg_001"],
        }
        self.prompt = ""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Validate adapter arguments and return configured schema data."""

        self.prompt = prompt
        assert output_model is RollingSummary
        assert model_name == "summary-model"
        assert metadata == {"agent": "rolling_summarizer"}
        return output_model.model_validate(self.result_data)


class FailingClient:
    """Structured client that simulates a model or parsing failure."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Raise so the summarizer must use its deterministic fallback."""

        _ = (prompt, output_model, model_name, metadata)
        raise RuntimeError("summary model unavailable")


@pytest.mark.asyncio
async def test_model_backed_success_uses_current_client_and_local_control_fields() -> None:
    """The model supplies content while the application controls identity and coverage."""

    payload = rolling_summarizer_input()
    client = ScriptedClient()
    result = await RollingSummarizer(
        client,
        model_name="summary-model",
    ).summarize(payload)

    assert result.current_problem == "model-backed summary"
    assert result.session_id == payload.session_id
    assert result.summary_version == 1
    assert result.covered_from == payload.uncovered_messages[0].id
    assert result.covered_to == payload.uncovered_messages[-1].id
    for field in (
        "INPUT_JSON",
        "previous_summary",
        "uncovered_messages",
        "current_state",
        "interventions",
    ):
        assert field in client.prompt


@pytest.mark.asyncio
async def test_model_backed_merges_previous_grounded_content() -> None:
    """A model response cannot silently discard already grounded list content."""

    messages = work_stress_dialogue()
    previous = RollingSummary(
        session_id=messages[0].session_id,
        summary_version=1,
        covered_from=messages[0].id,
        covered_to=messages[1].id,
        important_user_statements=[messages[0].content],
        source_message_ids=[messages[0].id],
    )
    payload = RollingSummarizerInput(
        session_id=messages[0].session_id,
        previous_summary=previous,
        uncovered_messages=messages[2:4],
        current_state=empty_state(messages[0].session_id),
    )
    client = ScriptedClient(
        {
            "session_id": "wrong-session",
            "summary_version": 99,
            "covered_from": "msg_001",
            "covered_to": "msg_003",
            "important_user_statements": [messages[2].content],
            "source_message_ids": ["msg_003"],
        }
    )

    result = await RollingSummarizer(
        client,
        model_name="summary-model",
    ).summarize(payload)

    assert result.important_user_statements == [messages[0].content, messages[2].content]
    assert result.source_message_ids == [messages[0].id, messages[2].id]
    assert result.summary_version == 2


@pytest.mark.asyncio
async def test_model_unknown_source_falls_back() -> None:
    """A source ID outside the typed payload must reject the whole model result."""

    payload = rolling_summarizer_input()
    client = ScriptedClient(
        {
            "session_id": "model-session",
            "summary_version": 99,
            "covered_from": "msg_001",
            "covered_to": "msg_003",
            "current_problem": "invalid model fact",
            "source_message_ids": ["msg_not_in_input"],
        }
    )

    result = await RollingSummarizer(
        client,
        model_name="summary-model",
    ).summarize(payload)

    assert result.current_problem != "invalid model fact"
    assert MessageId("msg_not_in_input") not in result.source_message_ids


@pytest.mark.asyncio
async def test_model_factual_content_without_sources_falls_back() -> None:
    """The empty-list all() case must not admit an untraceable model fact."""

    payload = rolling_summarizer_input()
    client = ScriptedClient(
        {
            "session_id": "model-session",
            "summary_version": 99,
            "covered_from": "msg_001",
            "covered_to": "msg_003",
            "current_problem": "untraceable fact",
            "source_message_ids": [],
        }
    )

    result = await RollingSummarizer(
        client,
        model_name="summary-model",
    ).summarize(payload)

    assert result.current_problem != "untraceable fact"
    assert result.source_message_ids


@pytest.mark.asyncio
async def test_model_invalid_coverage_relation_falls_back() -> None:
    """Unknown or reversed model coverage endpoints should trigger fallback."""

    payload = rolling_summarizer_input()
    client = ScriptedClient(
        {
            "session_id": "model-session",
            "summary_version": 99,
            "covered_from": "msg_005",
            "covered_to": "msg_001",
            "current_problem": "invalid coverage fact",
            "source_message_ids": ["msg_001"],
        }
    )

    result = await RollingSummarizer(
        client,
        model_name="summary-model",
    ).summarize(payload)

    assert result.current_problem != "invalid coverage fact"


@pytest.mark.asyncio
async def test_model_client_exception_falls_back() -> None:
    """Client failures must not interrupt summary processing."""

    payload = rolling_summarizer_input()
    expected = await FakeRollingSummarizer().summarize(payload)

    result = await RollingSummarizer(FailingClient()).summarize(payload)

    assert result == expected


@pytest.mark.asyncio
async def test_model_client_missing_uses_fake_fallback() -> None:
    """A missing client should preserve the deterministic implementation."""

    payload = rolling_summarizer_input()
    expected = await FakeRollingSummarizer().summarize(payload)

    assert await RollingSummarizer().summarize(payload) == expected
