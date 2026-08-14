"""Deterministic, schema-only conflict detection for proposed memories."""

import re

from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryConflict,
    MemoryOperation,
)

DUPLICATE_MEMORY = "duplicate_memory"
REINFORCES_EXISTING_MEMORY = "reinforces_existing_memory"
CONFLICTS_WITH_EXISTING_MEMORY = "conflicts_with_existing_memory"
SUPERSEDES_EXISTING_MEMORY = "supersedes_existing_memory"

_SMALL_STEP_TERMS = (
    "small step",
    "one small step",
    "one step at a time",
    "one at a time",
    "\u5c0f\u6b65\u9aa4",
    "\u4e00\u4e2a\u5c0f\u6b65\u9aa4",
    "\u4e00\u6b65\u4e00\u6b65",
    "\u6bcf\u6b21\u4e00\u4e2a",
)
_COMPLETE_PLAN_TERMS = (
    "complete plan",
    "full plan",
    "all steps",
    "all at once",
    "\u5b8c\u6574\u65b9\u6848",
    "\u5b8c\u6574\u8ba1\u5212",
    "\u4e00\u6b21\u7ed9\u5b8c\u6574",
    "\u4e00\u6b21\u6027",
)
_CONTINUE_TERMS = (
    "continue",
    "keep talking",
    "talk more",
    "\u7ee7\u7eed\u804a",
    "\u540e\u7eed\u7ee7\u7eed",
)
_STOP_TERMS = (
    "stop talking",
    "do not continue",
    "don't continue",
    "no longer want to discuss",
    "\u4e0d\u60f3\u7ee7\u7eed\u804a",
    "\u4e0d\u518d\u7ee7\u7eed",
    "\u5148\u4e0d\u804a",
)
_UPDATE_SIGNALS = (
    "now",
    "instead",
    "from now on",
    "change to",
    "switch to",
    "\u73b0\u5728",
    "\u4ee5\u540e",
    "\u6539\u6210",
    "\u6362\u6210",
    "\u5148\u4e0d\u8981",
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ConflictResolver:
    """Detect duplicate, reinforcing, conflicting, and superseding memories."""

    def detect(
        self,
        candidate: MemoryCandidate,
        existing_memories: list[LongTermMemory],
    ) -> list[MemoryConflict]:
        """Return relation records between one candidate and existing memories."""

        candidate_text = _normalize_text(candidate.content)
        conflicts: list[MemoryConflict] = []
        for memory in existing_memories:
            memory_text = _normalize_text(memory.content)
            if not _is_related(candidate, candidate_text, memory, memory_text):
                continue
            relation = _relation_for(candidate, candidate_text, memory, memory_text)
            if relation is not None:
                conflicts.append(relation)
        return conflicts


def detect_memory_conflicts(
    candidate: MemoryCandidate,
    existing_memories: list[LongTermMemory],
) -> list[MemoryConflict]:
    """Convenience function for callers that do not need a service instance."""

    return ConflictResolver().detect(candidate, existing_memories)


def _relation_for(
    candidate: MemoryCandidate,
    candidate_text: str,
    memory: LongTermMemory,
    memory_text: str,
) -> MemoryConflict | None:
    if _is_duplicate(candidate, candidate_text, memory, memory_text):
        return _conflict(candidate, memory, DUPLICATE_MEMORY, MemoryOperation.REINFORCE)
    if _is_supersede(candidate_text, memory_text):
        return _conflict(
            candidate,
            memory,
            SUPERSEDES_EXISTING_MEMORY,
            MemoryOperation.SUPERSEDE,
        )
    if _is_conflict(candidate_text, memory_text):
        return _conflict(
            candidate,
            memory,
            CONFLICTS_WITH_EXISTING_MEMORY,
            MemoryOperation.MARK_CONFLICT,
        )
    if _is_reinforcement(candidate, candidate_text, memory, memory_text):
        return _conflict(
            candidate,
            memory,
            REINFORCES_EXISTING_MEMORY,
            MemoryOperation.REINFORCE,
        )
    return None


def _conflict(
    candidate: MemoryCandidate,
    memory: LongTermMemory,
    reason: str,
    operation: MemoryOperation,
) -> MemoryConflict:
    return MemoryConflict(
        candidate=candidate,
        existing_memory_id=memory.id,
        reason=reason,
        recommended_operation=operation,
    )


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def _is_related(
    candidate: MemoryCandidate,
    candidate_text: str,
    memory: LongTermMemory,
    memory_text: str,
) -> bool:
    return (
        candidate.candidate_type == memory.memory_type
        or _same_preference_topic(candidate_text, memory_text)
        or bool(_keywords(candidate_text) & _keywords(memory_text))
    )


def _is_duplicate(
    candidate: MemoryCandidate,
    candidate_text: str,
    memory: LongTermMemory,
    memory_text: str,
) -> bool:
    return (
        candidate.candidate_type == memory.memory_type
        and not _has_opposing_polarity(candidate_text, memory_text)
        and (
            candidate_text == memory_text
            or candidate_text in memory_text
            or memory_text in candidate_text
        )
    )


def _is_reinforcement(
    candidate: MemoryCandidate,
    candidate_text: str,
    memory: LongTermMemory,
    memory_text: str,
) -> bool:
    return (
        candidate.candidate_type == memory.memory_type
        and not _has_opposing_polarity(candidate_text, memory_text)
        and _keyword_overlap_ratio(candidate_text, memory_text) >= 0.5
    )


def _is_conflict(candidate_text: str, memory_text: str) -> bool:
    return _same_preference_topic(candidate_text, memory_text) and _has_opposing_polarity(
        candidate_text,
        memory_text,
    )


def _is_supersede(candidate_text: str, memory_text: str) -> bool:
    return (
        _mentions_any(candidate_text, _UPDATE_SIGNALS)
        and _same_preference_topic(candidate_text, memory_text)
        and _has_opposing_polarity(candidate_text, memory_text)
    )


def _same_preference_topic(left: str, right: str) -> bool:
    step_topic = (_mentions_small_steps(left) or _mentions_complete_plan(left)) and (
        _mentions_small_steps(right) or _mentions_complete_plan(right)
    )
    continuation_topic = (_mentions_continue(left) or _mentions_stop(left)) and (
        _mentions_continue(right) or _mentions_stop(right)
    )
    return step_topic or continuation_topic


def _has_opposing_polarity(left: str, right: str) -> bool:
    return (
        (_mentions_small_steps(left) and _mentions_complete_plan(right))
        or (_mentions_small_steps(right) and _mentions_complete_plan(left))
        or (_mentions_continue(left) and _mentions_stop(right))
        or (_mentions_continue(right) and _mentions_stop(left))
    )


def _keyword_overlap_ratio(left: str, right: str) -> float:
    left_keywords = _keywords(left)
    right_keywords = _keywords(right)
    if not left_keywords or not right_keywords:
        return 0.0
    return len(left_keywords & right_keywords) / min(
        len(left_keywords),
        len(right_keywords),
    )


def _keywords(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text)) | {
        term for term in (*_SMALL_STEP_TERMS, *_COMPLETE_PLAN_TERMS) if term in text
    }


def _mentions_small_steps(text: str) -> bool:
    return _mentions_any(text, _SMALL_STEP_TERMS)


def _mentions_complete_plan(text: str) -> bool:
    return _mentions_any(text, _COMPLETE_PLAN_TERMS)


def _mentions_continue(text: str) -> bool:
    return _mentions_any(text, _CONTINUE_TERMS)


def _mentions_stop(text: str) -> bool:
    return _mentions_any(text, _STOP_TERMS)


def _mentions_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def _contains_term(text: str, term: str) -> bool:
    """Use token boundaries for English and explicit substrings for Chinese."""

    if term.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return term in text


__all__ = [
    "CONFLICTS_WITH_EXISTING_MEMORY",
    "DUPLICATE_MEMORY",
    "REINFORCES_EXISTING_MEMORY",
    "SUPERSEDES_EXISTING_MEMORY",
    "ConflictResolver",
    "detect_memory_conflicts",
]
