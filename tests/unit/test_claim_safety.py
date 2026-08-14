"""Unit tests for the shared deterministic unsupported-claim detector."""

import pytest

from services.claim_safety import (
    HIDDEN_MOTIVE_INFERENCE,
    UNSUPPORTED_DIAGNOSIS,
    UNSUPPORTED_PERSONALITY_JUDGMENT,
    UNSUPPORTED_TREATMENT_OUTCOME,
    first_unsupported_claim_reason,
    unsupported_claim_reason,
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("The user has an anxiety disorder.", UNSUPPORTED_DIAGNOSIS),
        ("The user has a personality disorder.", UNSUPPORTED_PERSONALITY_JUDGMENT),
        ("The user has a hidden motive.", HIDDEN_MOTIVE_INFERENCE),
        ("The user has fully recovered.", UNSUPPORTED_TREATMENT_OUTCOME),
        ("用户有焦虑症。", UNSUPPORTED_DIAGNOSIS),
        ("用户有回避型人格。", UNSUPPORTED_PERSONALITY_JUDGMENT),
        ("用户存在隐藏动机。", HIDDEN_MOTIVE_INFERENCE),
        ("用户已经改善了。", UNSUPPORTED_TREATMENT_OUTCOME),
    ],
)
def test_detects_each_reason_in_english_and_chinese(text: str, reason: str) -> None:
    assert unsupported_claim_reason(text) == reason


def test_normalizes_unicode_case_and_repeated_whitespace() -> None:
    assert (
        unsupported_claim_reason("ＴＨＥ USER has an ANXIETY\t  DISORDER.")
        == UNSUPPORTED_DIAGNOSIS
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \t\n",
        "The user feels anxious today.",
        "用户今天感到焦虑和压力。",
        "用户今天感到难过。",
        "The connection was secured.",
        "Aptsdword has only embedded letters.",
    ],
)
def test_safe_or_empty_text_returns_none(text: str) -> None:
    assert unsupported_claim_reason(text) is None


def test_depression_remains_a_conservative_compatibility_match() -> None:
    assert (
        unsupported_claim_reason(
            "The discussion is about historical economic depression."
        )
        == UNSUPPORTED_DIAGNOSIS
    )


def test_multiple_texts_return_fixed_highest_priority_reason() -> None:
    assert (
        first_unsupported_claim_reason(
            ["The user has fully recovered.", "The user has bipolar."]
        )
        == UNSUPPORTED_DIAGNOSIS
    )


def test_single_text_uses_fixed_category_priority() -> None:
    assert (
        unsupported_claim_reason(
            "The hidden motive follows a diagnosed personality disorder."
        )
        == UNSUPPORTED_DIAGNOSIS
    )


def test_calls_do_not_share_state() -> None:
    assert unsupported_claim_reason("The user has PTSD.") == UNSUPPORTED_DIAGNOSIS
    assert unsupported_claim_reason("The user likes concise replies.") is None
    assert first_unsupported_claim_reason(["ordinary text"]) is None


@pytest.mark.parametrize(
    "text",
    [
        "用户被诊断为PTSD。",
        "用户有PTSD症状。",
        "用户有anxiety disorder。",
        "The user有PTSD。",
        "用户存在bipolar风险。",
        "用户被诊断为ＰＴＳＤ。",
    ],
)
def test_english_diagnosis_terms_can_touch_chinese_text(text: str) -> None:
    assert unsupported_claim_reason(text) == UNSUPPORTED_DIAGNOSIS


@pytest.mark.parametrize(
    "text",
    [
        "Aptsdword",
        "ptsdlike",
        "my_ptsd_note",
        "secured",
        "bipolarity",
        "narcissistically",
        "用户今天感到焦虑和压力。",
        "The user feels anxious today.",
    ],
)
def test_english_terms_embedded_in_ascii_words_do_not_match(text: str) -> None:
    assert unsupported_claim_reason(text) is None


def test_multiple_texts_detect_mixed_chinese_and_english_claims() -> None:
    assert (
        first_unsupported_claim_reason(["safe", "用户有PTSD症状。"])
        == UNSUPPORTED_DIAGNOSIS
    )
