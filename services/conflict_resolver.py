"""Deterministic, schema-only conflict detection for proposed memories."""

import re

from schemas.memory import (
    LongTermMemory,
    MemoryCandidate,
    MemoryConflict,
    MemoryOperation,
)
from services.embedding_service import SimilarityService, create_similarity_service

DUPLICATE_MEMORY = "duplicate_memory"
REINFORCES_EXISTING_MEMORY = "reinforces_existing_memory"
CONFLICTS_WITH_EXISTING_MEMORY = "conflicts_with_existing_memory"
SUPERSEDES_EXISTING_MEMORY = "supersedes_existing_memory"
SEMANTIC_DUPLICATE_MEMORY = "semantic_duplicate_memory"
COMPLEMENTARY_MEMORY = "complementary_memory"
ATTRIBUTE_VALUE_CONFLICT = "attribute_value_conflict"
ATTRIBUTE_VALUE_SUPERSEDE = "attribute_value_supersede"

# Cosine thresholds for the write-side similarity layer. Initial values must
# be calibrated against real conversation data on the server before tuning.
SEMANTIC_DUPLICATE_SIMILARITY = 0.92
SAME_TOPIC_SIMILARITY = 0.75

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
_COMPLEMENTARY_SIGNALS = ("另外", "此外", "同时", "也", "并且", "还有", "also", "additionally")
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_AGE_RE = re.compile(r"(?:今年|现在)?\s*(\d{1,3})\s*岁")
_AGE_YEAR_RE = re.compile(r"今年\s*(\d{1,3})(?!\s*(?:岁|月)|\d)")
_AGE_EN_RE = re.compile(r"\b(\d{1,3})\s+years?\s+old\b")
_YEAR_RE = re.compile(r"(\d{4})\s*年")
_MONTH_RE = re.compile(r"(\d{1,2})\s*月")
_MONEY_RE = re.compile(r"(?:[¥￥]\s*)?(\d+(?:\.\d+)?)\s*(?:万|千)?\s*元")
_NAME_RE = re.compile(r"(?:我叫|我的名字是|我是)\s*([一-鿿A-Za-z]{2,6})")
_LOCATION_RE = re.compile(
    r"(?:住在|搬到|来自|定居在|出生于|老家在|家乡在|家在)\s*([一-鿿A-Za-z]{1,6})"
)
_LIKE_DISLIKE_RE = re.compile(
    r"(?:喜欢|爱|讨厌|不喜欢|不再喜欢|不爱)\s*([一-鿿\d]{1,20})"
)
_SUBJECT_ANCHOR_RE = re.compile(
    r"([一-鿿]{2,4})(?=\d*(?:今年|现在|已经|岁|住在|来自|搬到|说|是|的))"
)


class ConflictResolver:
    """Detect duplicate, reinforcing, conflicting, and superseding memories."""

    def __init__(
        self,
        similarity_service: SimilarityService | None = None,
    ) -> None:
        """Create a resolver with an injectable cosine similarity service."""

        self._similarity_service = (
            similarity_service or create_similarity_service()
        )

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
            similarity = self._similarity_service.similarity(
                candidate_text,
                memory_text,
            )
            relation = _relation_for(
                candidate,
                candidate_text,
                memory,
                memory_text,
                similarity,
            )
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
    similarity: float,
) -> MemoryConflict | None:
    if _is_duplicate(candidate, candidate_text, memory, memory_text):
        return _conflict(candidate, memory, DUPLICATE_MEMORY, MemoryOperation.REINFORCE)
    attribute_relation = _attribute_relation(candidate_text, memory_text)
    if attribute_relation == MemoryOperation.SUPERSEDE:
        return _conflict(
            candidate,
            memory,
            ATTRIBUTE_VALUE_SUPERSEDE,
            MemoryOperation.SUPERSEDE,
        )
    if attribute_relation == MemoryOperation.MARK_CONFLICT:
        return _conflict(
            candidate,
            memory,
            ATTRIBUTE_VALUE_CONFLICT,
            MemoryOperation.MARK_CONFLICT,
        )
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
    if _is_like_dislike_conflict(candidate_text, memory_text):
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
    if similarity >= SEMANTIC_DUPLICATE_SIMILARITY:
        return _conflict(
            candidate,
            memory,
            SEMANTIC_DUPLICATE_MEMORY,
            MemoryOperation.REINFORCE,
        )
    if similarity >= SAME_TOPIC_SIMILARITY:
        return _conflict(
            candidate,
            memory,
            (
                COMPLEMENTARY_MEMORY
                if _mentions_any(candidate_text, _COMPLEMENTARY_SIGNALS)
                else REINFORCES_EXISTING_MEMORY
            ),
            (
                MemoryOperation.MERGE
                if _mentions_any(candidate_text, _COMPLEMENTARY_SIGNALS)
                else MemoryOperation.REINFORCE
            ),
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


def _attribute_relation(candidate_text: str, memory_text: str) -> MemoryOperation | None:
    """Return SUPERSEDE or MARK_CONFLICT when the same attribute changed value.

    Requires shared subject evidence so distinct people with the same
    attribute key (Xiao Ming 40 vs Xiao Hong 18) are not treated as one
    conflicting fact. An explicit update signal in the candidate means the
    user is correcting the old value; otherwise the pair is marked for
    review instead of silently overwriting.
    """

    if not _shared_subject(candidate_text, memory_text):
        return None
    candidate_pairs = _attribute_pairs(candidate_text)
    memory_pairs = _attribute_pairs(memory_text)
    for key in set(candidate_pairs) & set(memory_pairs):
        if candidate_pairs[key] != memory_pairs[key]:
            if _mentions_any(candidate_text, _UPDATE_SIGNALS):
                return MemoryOperation.SUPERSEDE
            return MemoryOperation.MARK_CONFLICT
    return None


def _attribute_pairs(text: str) -> dict[str, str]:
    """Extract recognized attribute key/value pairs from a memory statement."""

    pairs: dict[str, str] = {}
    age = _AGE_RE.search(text) or _AGE_YEAR_RE.search(text) or _AGE_EN_RE.search(text)
    if age is not None:
        pairs["age"] = age.group(1)
    year = _YEAR_RE.search(text)
    if year is not None:
        pairs["year"] = year.group(1)
    month = _MONTH_RE.search(text)
    if month is not None:
        pairs["month"] = month.group(1)
    money = _MONEY_RE.search(text)
    if money is not None:
        pairs["money"] = money.group(1)
    location = _LOCATION_RE.search(text)
    if location is not None:
        pairs["location"] = location.group(1)
    name = _NAME_RE.search(text)
    if name is not None:
        pairs["name"] = name.group(1)
    return pairs


def _shared_subject(left: str, right: str) -> bool:
    """Return whether both statements plausibly describe the same person."""

    if ("我" in left and "我" in right) or ("user" in left and "user" in right):
        return True

    def _subjects(text: str) -> set[str]:
        candidates: set[str] = set()
        for match in _SUBJECT_ANCHOR_RE.finditer(text):
            subject = match.group(1)
            candidates.add(subject)
            if len(subject) > 2:
                candidates.add(subject[:2])
        return candidates

    left_subjects = _subjects(left)
    right_subjects = _subjects(right)
    return bool(left_subjects & right_subjects)


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


def _is_like_dislike_conflict(left: str, right: str) -> bool:
    """Return True when both texts describe the same subject with opposite valence."""

    left_obj = _like_dislike_object(left)
    right_obj = _like_dislike_object(right)
    if left_obj is None or right_obj is None or left_obj != right_obj:
        return False
    left_likes = _has_like(left) and not _has_dislike(left)
    right_dislikes = _has_dislike(right) and not _has_like(right)
    right_likes = _has_like(right) and not _has_dislike(right)
    left_dislikes = _has_dislike(left) and not _has_like(left)
    return (left_likes and right_dislikes) or (left_dislikes and right_likes)


def _like_dislike_object(text: str) -> str | None:
    match = _LIKE_DISLIKE_RE.search(text)
    return match.group(1) if match is not None else None


def _has_like(text: str) -> bool:
    return any(term in text for term in ("喜欢", "爱", "like"))


def _has_dislike(text: str) -> bool:
    return any(term in text for term in ("讨厌", "不喜欢", "不再喜欢", "不爱", "dislike", "hate"))


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
    "ATTRIBUTE_VALUE_CONFLICT",
    "ATTRIBUTE_VALUE_SUPERSEDE",
    "COMPLEMENTARY_MEMORY",
    "CONFLICTS_WITH_EXISTING_MEMORY",
    "DUPLICATE_MEMORY",
    "REINFORCES_EXISTING_MEMORY",
    "SEMANTIC_DUPLICATE_MEMORY",
    "SEMANTIC_DUPLICATE_SIMILARITY",
    "SAME_TOPIC_SIMILARITY",
    "SUPERSEDES_EXISTING_MEMORY",
    "ConflictResolver",
    "detect_memory_conflicts",
]
