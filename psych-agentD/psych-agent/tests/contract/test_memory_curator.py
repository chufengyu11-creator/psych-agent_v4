"""Contract tests for deterministic and model-backed memory curation."""

from collections.abc import Mapping

import pytest

from agents.memory_curator import FakeMemoryCurator, MemoryCurator, MemoryCuratorInput
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId, SourceReference
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.state import SessionState, StateItem
from schemas.summary import SessionFinalizerResult

SESSION_ID = SessionId("memory-curator-contract")


def _message(
    message_id: str,
    role: MessageRole,
    content: str,
    sequence_number: int = 1,
) -> Message:
    return Message(
        id=MessageId(message_id),
        session_id=SESSION_ID,
        role=role,
        content=content,
        sequence_number=sequence_number,
    )


def _candidate_data(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "candidate_type": "interaction_preference",
        "content": "User preference: one small step at a time.",
        "source_type": "explicit_user_statement",
        "confidence": 0.9,
        "requires_user_confirmation": False,
        "sensitivity": "low",
        "recommended_operation": "CREATE",
        "evidence": [
            {
                "message_id": "msg_user",
                "quote": "Please give me one small step at a time.",
            }
        ],
        "provisional_candidate_indexes": [],
    }
    data.update(updates)
    return data


def _candidate(message_id: str = "msg_user") -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content="User preference: one small step at a time.",
        source_message_ids=[MessageId(message_id)],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _payload(
    *,
    messages: list[Message] | None = None,
    final_state: SessionState | None = None,
    finalizer_candidates: list[MemoryCandidate] | None = None,
) -> MemoryCuratorInput:
    return MemoryCuratorInput(
        session_id=SESSION_ID,
        messages=messages or [],
        final_state=final_state or SessionState(session_id=SESSION_ID),
        finalizer_result=SessionFinalizerResult(
            session_id=SESSION_ID,
            session_summary="Grounded close result",
            candidate_memories=finalizer_candidates or [],
        ),
    )


class ScriptedClient:
    def __init__(self, candidates: list[dict[str, object]], *, fail: bool = False) -> None:
        self.candidates = candidates
        self.fail = fail
        self.prompt = ""
        self.output_model_name = ""
        self.model_name: str | None = None

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        self.prompt = prompt
        self.output_model_name = output_model.__name__
        self.model_name = model_name
        assert metadata == {"agent": "memory_curator"}
        if self.fail:
            raise RuntimeError("curator unavailable")
        return output_model.model_validate({"candidates": self.candidates})


def _user_payload() -> MemoryCuratorInput:
    return _payload(
        messages=[
            _message(
                "msg_user",
                MessageRole.USER,
                "Please give me one small step at a time.",
            ),
            _message("msg_assistant", MessageRole.ASSISTANT, "One small step.", 2),
        ]
    )


@pytest.mark.asyncio
async def test_fake_extracts_explicit_user_small_step_preference() -> None:
    result = await FakeMemoryCurator().curate(_user_payload())
    assert len(result) == 1
    assert result[0].candidate_type == MemoryType.INTERACTION_PREFERENCE
    assert result[0].source_message_ids == [MessageId("msg_user")]
    assert result[0].recommended_operation == MemoryOperation.CREATE


@pytest.mark.asyncio
async def test_fake_ignores_assistant_only_preference() -> None:
    payload = _payload(
        messages=[
            _message(
                "msg_assistant",
                MessageRole.ASSISTANT,
                "I will give one small step at a time.",
            )
        ]
    )
    assert await FakeMemoryCurator().curate(payload) == []


@pytest.mark.asyncio
async def test_fake_uses_sourced_state_preference() -> None:
    state = SessionState(
        session_id=SESSION_ID,
        user_preferences=[
            StateItem(
                value="one step at a time",
                source=SourceReference(message_id=MessageId("msg_state")),
            )
        ],
    )
    result = await FakeMemoryCurator().curate(_payload(final_state=state))
    assert result[0].source_message_ids == [MessageId("msg_state")]


@pytest.mark.asyncio
async def test_fake_keeps_grounded_finalizer_candidate() -> None:
    candidate = _candidate()
    result = await FakeMemoryCurator().curate(
        _payload(
            messages=[_message("msg_user", MessageRole.USER, "Grounded text")],
            finalizer_candidates=[candidate],
        )
    )
    assert result == [candidate]


@pytest.mark.asyncio
async def test_fake_discards_unknown_finalizer_source() -> None:
    result = await FakeMemoryCurator().curate(
        _payload(
            messages=[_message("msg_user", MessageRole.USER, "Grounded text")],
            finalizer_candidates=[_candidate("unknown")],
        )
    )
    assert result == []


@pytest.mark.asyncio
async def test_fake_merges_duplicate_candidate_sources_in_order() -> None:
    first = _candidate("msg_user")
    second = _candidate("msg_second")
    result = await FakeMemoryCurator().curate(
        _payload(
            messages=[
                _message("msg_user", MessageRole.USER, "Grounded"),
                _message("msg_second", MessageRole.USER, "Grounded too", 2),
            ],
            finalizer_candidates=[first, second],
        )
    )
    assert len(result) == 1
    assert result[0].source_message_ids == [
        MessageId("msg_user"),
        MessageId("msg_second"),
    ]


@pytest.mark.asyncio
async def test_too_many_meetings_is_not_a_guidance_preference() -> None:
    payload = _payload(
        messages=[
            _message(
                "msg_user",
                MessageRole.USER,
                "I have too many meetings this week.",
            )
        ]
    )
    assert await FakeMemoryCurator().curate(payload) == []


@pytest.mark.asyncio
async def test_model_success_uses_private_envelope_and_current_client_contract() -> None:
    client = ScriptedClient([_candidate_data()])
    result = await MemoryCurator(client, model_name="curator-model").curate(
        _user_payload()
    )
    assert client.output_model_name == "_RawMemoryCandidateBatch"
    assert client.model_name == "curator-model"
    assert "INPUT_JSON" in client.prompt
    for field in ("messages", "final_state", "finalizer_result", "rolling_summary"):
        assert field in client.prompt
    assert isinstance(result, list)
    assert result[0].source_message_ids == [MessageId("msg_user")]


@pytest.mark.asyncio
async def test_missing_client_matches_fake_fallback() -> None:
    payload = _user_payload()
    assert await MemoryCurator().curate(payload) == await FakeMemoryCurator().curate(
        payload
    )


@pytest.mark.asyncio
async def test_client_exception_matches_fake_fallback() -> None:
    payload = _user_payload()
    result = await MemoryCurator(ScriptedClient([], fail=True)).curate(payload)
    assert result == await FakeMemoryCurator().curate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates"),
    [
        {"evidence": [{"message_id": "unknown", "quote": "x"}]},
        {"evidence": []},
        {
            "evidence": [{"message_id": "msg_assistant", "quote": "One small step."}],
            "source_type": "explicit_user_statement",
        },
        {"source_type": "repeated_observation"},
        {"source_type": "model_inference"},
        {"recommended_operation": "REINFORCE"},
        {"content": "The user has an anxiety disorder."},
        {"content": "The user has a personality disorder."},
        {"content": "The user's hidden motive is avoidance."},
        {"content": "Treatment succeeded and the user fully recovered."},
        {"content": "用户有焦虑症。"},
        {"content": "用户已经改善了。"},
        {"content": "用户存在隐藏动机。"},
    ],
)
async def test_unsafe_model_candidate_rejects_only_that_candidate(
    updates: dict[str, object],
) -> None:
    payload = _user_payload()
    safe_but_distinct = _candidate_data(
        content="User preference: one small step at a time."
    )
    unsafe = _candidate_data(**updates)
    result = await MemoryCurator(
        ScriptedClient([safe_but_distinct, unsafe])
    ).curate(payload)
    assert len(result) == 1
    assert result[0].content == "User preference: one small step at a time."
