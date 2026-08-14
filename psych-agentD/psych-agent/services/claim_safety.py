"""Deterministic detection of unsupported summary and memory claims."""

import re
import unicodedata
from collections.abc import Iterable

UNSUPPORTED_DIAGNOSIS = "unsupported_diagnosis"
UNSUPPORTED_PERSONALITY_JUDGMENT = "unsupported_personality_judgment"
HIDDEN_MOTIVE_INFERENCE = "hidden_motive_inference"
UNSUPPORTED_TREATMENT_OUTCOME = "unsupported_treatment_outcome"

_CLAIM_TERMS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        UNSUPPORTED_DIAGNOSIS,
        (
            "diagnosed",
            "depression",
            "bipolar",
            "ptsd",
            "anxiety disorder",
            "depressive disorder",
        ),
        ("焦虑症", "抑郁症", "双相情感障碍", "创伤后应激障碍"),
    ),
    (
        UNSUPPORTED_PERSONALITY_JUDGMENT,
        (
            "personality disorder",
            "avoidant personality",
            "narcissistic",
            "dependent personality",
        ),
        ("人格障碍", "回避型人格"),
    ),
    (
        HIDDEN_MOTIVE_INFERENCE,
        (
            "hidden motive",
            "unconscious motive",
            "actually avoiding",
            "really just avoiding",
        ),
        ("隐藏动机", "潜意识", "潜意识动机", "其实是在逃避"),
    ),
    (
        UNSUPPORTED_TREATMENT_OUTCOME,
        (
            "cured",
            "treatment succeeded",
            "symptoms resolved",
            "fully recovered",
        ),
        ("治疗成功", "已经好转", "已经改善"),
    ),
)


def _normalize(text: str) -> str:
    """Normalize Unicode, case, and whitespace without changing the input."""

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _contains_english_term(text: str, term: str) -> bool:
    """Match an English term at ASCII boundaries, allowing adjacent CJK text."""

    return (
        re.search(
            rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])",
            text,
        )
        is not None
    )


def unsupported_claim_reason(text: str) -> str | None:
    """Return the highest-priority unsupported-claim reason for one text."""

    normalized = _normalize(text)
    if not normalized:
        return None
    for reason_code, english_terms, chinese_terms in _CLAIM_TERMS:
        if any(
            _contains_english_term(normalized, term) for term in english_terms
        ) or any(term in normalized for term in chinese_terms):
            return reason_code
    return None


def first_unsupported_claim_reason(texts: Iterable[str]) -> str | None:
    """Return the highest-priority reason found across all supplied texts."""

    normalized_texts = tuple(_normalize(text) for text in texts)
    for reason_code, english_terms, chinese_terms in _CLAIM_TERMS:
        if any(
            any(_contains_english_term(text, term) for term in english_terms)
            or any(term in text for term in chinese_terms)
            for text in normalized_texts
            if text
        ):
            return reason_code
    return None


__all__ = [
    "HIDDEN_MOTIVE_INFERENCE",
    "UNSUPPORTED_DIAGNOSIS",
    "UNSUPPORTED_PERSONALITY_JUDGMENT",
    "UNSUPPORTED_TREATMENT_OUTCOME",
    "first_unsupported_claim_reason",
    "unsupported_claim_reason",
]
