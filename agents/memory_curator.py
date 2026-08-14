"""Grounded fake and model-backed memory candidate curation."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import Field

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import ContractModel, MessageId, SessionId
from schemas.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemorySensitivity,
    MemorySourceType,
    MemoryType,
)
from schemas.messages import Message, MessageRole
from schemas.state import SessionState
from schemas.summary import RollingSummary, SessionFinalizerResult
from services.claim_safety import unsupported_claim_reason

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "memory_curator.md"

_SMALL_STEP_TERMS = (
    "每次一个小步骤",
    "每次只给我一个小步骤",
    "一次一个小步骤",
    "一步一步",
    "不要一次给太多建议",
    "不想一次收到太多建议",
    "one small step",
    "one step at a time",
    "too many suggestions at once",
    "too many steps at once",
    "do not give me too many suggestions",
)
_SMALL_STEP_CONTENT = "用户希望每次只收到一个小步骤，不要一次给太多建议。"

import re

_NAME_RE = re.compile(r"(?:我叫|我的名字是|我是)\s*([一-鰿A-Za-z]{2,6})")
_AGE_RE = re.compile(r"我(?:今年)?\s*(\d{1,3})\s*岁")
_LOCATION_RE = re.compile(r"(?:我老家在|我家在|我住在|我来自|我的家乡在|我的老家在)\s*([一-鰿A-Za-z]{1,10})")
_PREFERENCE_RE = re.compile(r"我(?:喜欢|爱|不喜欢|讨厌|不爱|不再喜欢)\s*(.{1,30})")
_PET_RE = re.compile(r"我养了.{0,5}(?:一只|一个|一条)?\s*(.{1,15})")

_EXPLICIT_FACT_PATTERNS: list[tuple[re.Pattern[str], MemoryType, str]] = [
    (_NAME_RE, MemoryType.SEMANTIC, "{fact}"),
    (_AGE_RE, MemoryType.SEMANTIC, "我今年{fact}岁"),
    (_LOCATION_RE, MemoryType.SEMANTIC, "我的老家在{fact}"),
    (_PET_RE, MemoryType.EPISODIC, "我养了{fact}"),
    (_PREFERENCE_RE, MemoryType.INTERACTION_PREFERENCE, "我{fact}"),
]


@dataclass(frozen=True)
class MemoryCuratorInput:
    """Typed input containing all evidence available during one session close."""

    session_id: SessionId
    messages: list[Message]
    final_state: SessionState
    finalizer_result: SessionFinalizerResult
    rolling_summary: RollingSummary | None = None


class _MemoryCandidateBatch(ContractModel):
    """Private structured-output envelope; it is not a public schema contract."""

    candidates: list[MemoryCandidate] = Field(default_factory=list)


class FakeMemoryCurator:
    """Deterministically curate grounded candidates without durable writes."""

    async def run(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        return await self.curate(payload)

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        """Merge safe finalizer, user-message, and sourced-state candidates."""

        candidates = list(payload.finalizer_result.candidate_memories)
        candidates.extend(_message_candidates(payload.messages))
        candidates.extend(_state_preference_candidates(payload.final_state))
        grounded = [
            candidate
            for candidate in candidates
            if _is_valid_candidate(candidate, payload)
        ]
        return _dedupe_candidates(grounded)


class MemoryCurator:
    """Model-backed curator with the deterministic curator as a safe fallback."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeMemoryCurator | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeMemoryCurator()
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback="Return grounded memory candidates as one JSON object.",
        )

    async def run(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        return await self.curate(payload)

    async def curate(self, payload: MemoryCuratorInput) -> list[MemoryCandidate]:
        """Curate with structured output, rejecting the whole unsafe batch."""

        if self._llm_client is None:
            return await self._fallback.curate(payload)
        try:
            batch = _MemoryCandidateBatch.model_validate(
                await self._llm_client.generate_structured(
                    self._build_prompt(payload),
                    _MemoryCandidateBatch,
                    model_name=self._model_name,
                    metadata={"agent": "memory_curator"},
                )
            )
        except Exception:
            return await self._fallback.curate(payload)
        if any(
            not _is_valid_candidate(candidate, payload) for candidate in batch.candidates
        ):
            return await self._fallback.curate(payload)
        fallback_candidates = await self._fallback.curate(payload)
        merged = list(batch.candidates)
        merged.extend(fallback_candidates)
        return _dedupe_candidates(merged)

    def _build_prompt(self, payload: MemoryCuratorInput) -> str:
        payload_json = json.dumps(
            {
                "session_id": payload.session_id,
                "messages": [message.model_dump(mode="json") for message in payload.messages],
                "final_state": payload.final_state.model_dump(mode="json"),
                "finalizer_result": payload.finalizer_result.model_dump(mode="json"),
                "rolling_summary": (
                    payload.rolling_summary.model_dump(mode="json")
                    if payload.rolling_summary is not None
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        return f"{self._prompt_template}\n\nINPUT_JSON:\n{payload_json}"


def _read_prompt(path: Path, *, fallback: str) -> str:
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _allowed_source_message_ids(payload: MemoryCuratorInput) -> set[MessageId]:
    ids = {message.id for message in payload.messages}
    ids.update(item.source.message_id for item in payload.final_state.active_topics)
    ids.update(item.source.message_id for item in payload.final_state.reported_emotions)
    ids.update(item.source.message_id for item in payload.final_state.user_preferences)
    ids.update(item.source.message_id for item in payload.final_state.open_questions)
    if payload.rolling_summary is not None:
        ids.update(payload.rolling_summary.source_message_ids)
    return ids


def _explicit_user_source_ids(payload: MemoryCuratorInput) -> set[MessageId]:
    ids = {
        message.id for message in payload.messages if message.role == MessageRole.USER
    }
    ids.update(
        preference.source.message_id
        for preference in payload.final_state.user_preferences
    )
    return ids


def _is_valid_candidate(
    candidate: MemoryCandidate,
    payload: MemoryCuratorInput,
) -> bool:
    if not candidate.content.strip() or not candidate.source_message_ids:
        return False
    if any(
        source_id not in _allowed_source_message_ids(payload)
        for source_id in candidate.source_message_ids
    ):
        return False
    if candidate.recommended_operation != MemoryOperation.CREATE:
        return False
    if candidate.source_type in {
        MemorySourceType.REPEATED_OBSERVATION,
        MemorySourceType.MODEL_INFERENCE,
    }:
        return False
    if (
        candidate.source_type == MemorySourceType.EXPLICIT_USER_STATEMENT
        and not set(candidate.source_message_ids) & _explicit_user_source_ids(payload)
    ):
        return False
    if unsupported_claim_reason(candidate.content) is not None:
        return False
    return not (
        (
            candidate.confidence < 0.8
            or candidate.sensitivity != MemorySensitivity.LOW
        )
        and not candidate.requires_user_confirmation
    )


def _message_candidates(messages: list[Message]) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for message in messages:
        if message.role != MessageRole.USER:
            continue
        content = _canonical_preference(message.content)
        if content is not None:
            candidates.append(_preference_candidate(content, message.id))
    return candidates


def _state_preference_candidates(final_state: SessionState) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []
    for preference in final_state.user_preferences:
        content = _canonical_preference(preference.value)
        if content is not None:
            candidates.append(_preference_candidate(content, preference.source.message_id))
    return candidates


def _canonical_preference(text: str) -> str | None:
    normalized = text.casefold()
    if any(term in normalized for term in _SMALL_STEP_TERMS):
        return _SMALL_STEP_CONTENT
    return None


def _preference_candidate(content: str, source_message_id: MessageId) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_type=MemoryType.INTERACTION_PREFERENCE,
        content=content,
        source_message_ids=[source_message_id],
        source_type=MemorySourceType.EXPLICIT_USER_STATEMENT,
        confidence=0.9,
        requires_user_confirmation=False,
        sensitivity=MemorySensitivity.LOW,
        recommended_operation=MemoryOperation.CREATE,
    )


def _dedupe_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    result: list[MemoryCandidate] = []
    key_indexes: dict[tuple[MemoryType, str], int] = {}
    for candidate in candidates:
        key = (candidate.candidate_type, _normalize_content(candidate.content))
        existing_index = key_indexes.get(key)
        if existing_index is not None:
            existing = result[existing_index]
            result[existing_index] = existing.model_copy(
                update={
                    "source_message_ids": _dedupe(
                        [*existing.source_message_ids, *candidate.source_message_ids]
                    )
                }
            )
            continue
        key_indexes[key] = len(result)
        result.append(
            candidate.model_copy(
                update={
                    "content": candidate.content.strip(),
                    "source_message_ids": _dedupe(candidate.source_message_ids),
                }
            )
        )
    return result


def _normalize_content(content: str) -> str:
    return " ".join(content.casefold().strip().split())


def _dedupe(items: list[ItemT]) -> list[ItemT]:
    result: list[ItemT] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


__all__ = ["FakeMemoryCurator", "MemoryCurator", "MemoryCuratorInput"]
