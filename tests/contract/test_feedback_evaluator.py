"""Contract tests for fake and model-backed feedback evaluators."""

import logging
from collections.abc import Mapping

import pytest

from agents.feedback_evaluator import FakeFeedbackEvaluator, FeedbackEvaluator
from llm.exceptions import LLMTimeoutError
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.feedback import (
    FeedbackInput,
    FeedbackLabel,
    FeedbackResult,
    ObjectiveProgress,
    StrategyFit,
)
from schemas.intervention import InterventionStatus
from schemas.messages import MessageRole
from services.model_execution import ModelExecutionFailure
from tests.fixtures.interventions import pending_reflective_intervention
from tests.fixtures.messages import make_message, work_stress_dialogue


def _feedback_input(user_text: str) -> FeedbackInput:
    """Build feedback input for the standard reflective intervention fixture."""

    dialogue = work_stress_dialogue()
    return FeedbackInput(
        previous_intervention=pending_reflective_intervention(),
        assistant_message=dialogue[1],
        next_user_message=make_message(
            "msg_feedback",
            MessageRole.USER,
            user_text,
            3,
        ),
    )


@pytest.mark.asyncio
async def test_fake_evaluator_classifies_explicit_rejection() -> None:
    """Explicit rejection of emotion analysis should be poor negative feedback."""

    result = await FakeFeedbackEvaluator().evaluate(
        _feedback_input("我不想继续分析情绪，我需要具体步骤。")
    )

    assert result.explicit_feedback == FeedbackLabel.NEGATIVE
    assert result.strategy_fit == StrategyFit.POOR
    assert result.objective_progress == ObjectiveProgress.NOT_ACHIEVED


@pytest.mark.asyncio
async def test_fake_evaluator_classifies_not_working_as_negative_feedback() -> None:
    """A user saying the tried method is not working should trigger a strategy switch."""

    result = await FakeFeedbackEvaluator().evaluate(_feedback_input("不太行，还是有压力。"))

    assert result.explicit_feedback == FeedbackLabel.NEGATIVE
    assert result.strategy_fit == StrategyFit.POOR
    assert result.objective_progress == ObjectiveProgress.NOT_ACHIEVED


@pytest.mark.asyncio
async def test_fake_evaluator_does_not_treat_continuation_as_positive() -> None:
    """Continuing to describe anxiety is not an evaluation of the prior strategy."""

    result = await FakeFeedbackEvaluator().evaluate(
        _feedback_input("今天开会的时候，我还是很紧张。")
    )

    assert result.explicit_feedback == FeedbackLabel.ABSENT
    assert result.strategy_fit == StrategyFit.UNKNOWN
    assert result.objective_progress == ObjectiveProgress.UNKNOWN


@pytest.mark.asyncio
async def test_fake_evaluator_classifies_explicit_helpfulness() -> None:
    """An explicit helpfulness statement should be positive feedback."""

    result = await FakeFeedbackEvaluator().evaluate(
        _feedback_input("谢谢，这个回应很有帮助。")
    )

    assert result.explicit_feedback == FeedbackLabel.POSITIVE
    assert result.strategy_fit == StrategyFit.GOOD
    assert result.objective_progress == ObjectiveProgress.ACHIEVED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    [
        "可以，但请一次只给我一个步骤。",
        "可以，而且建议不要一次给太多步骤。",
    ],
)
async def test_fake_evaluator_classifies_partial_acceptance_as_mixed(
    user_text: str,
) -> None:
    """Requests to adjust an accepted response must not become poor negatives."""

    result = await FakeFeedbackEvaluator().evaluate(_feedback_input(user_text))

    assert result.explicit_feedback == FeedbackLabel.MIXED
    assert result.strategy_fit == StrategyFit.MIXED
    assert result.objective_progress == ObjectiveProgress.PARTIAL


@pytest.mark.asyncio
async def test_concrete_steps_preference_alone_is_not_negative_feedback() -> None:
    """Requesting concrete steps after helpful feedback is not an explicit rejection."""

    result = await FakeFeedbackEvaluator().evaluate(
        _feedback_input("谢谢，这个回应有帮助，我想要下一步具体步骤。")
    )

    assert result.explicit_feedback in {FeedbackLabel.POSITIVE, FeedbackLabel.MIXED}
    assert result.strategy_fit in {StrategyFit.GOOD, StrategyFit.MIXED}
    assert result.objective_progress != ObjectiveProgress.NOT_ACHIEVED


@pytest.mark.asyncio
async def test_english_ok_substring_does_not_create_mixed_feedback() -> None:
    """The letters 'ok' inside smoking must not count as partial acceptance."""

    result = await FakeFeedbackEvaluator().evaluate(
        _feedback_input("I am smoking more lately, but work is still stressful.")
    )

    assert result.explicit_feedback == FeedbackLabel.ABSENT
    assert result.strategy_fit == StrategyFit.UNKNOWN
    assert result.objective_progress == ObjectiveProgress.UNKNOWN


class SuccessfulStructuredClient:
    """Structured client returning a valid feedback result."""

    def __init__(self) -> None:
        self.prompt = ""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Capture the adapter call and return schema-valid model output."""

        self.prompt = prompt
        assert model_name == "feedback-model"
        assert metadata == {"agent": "feedback_evaluator"}
        return output_model.model_validate(
            {
                "explicit_feedback": "positive",
                "objective_progress": "achieved",
                "strategy_fit": "good",
                "confidence": 0.9,
                "evidence_message_id": "msg_feedback",
                "evidence_quote": "很有帮助",
            }
        )


class FailingStructuredClient:
    """Structured client simulating model or parsing failure."""

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Raise so FeedbackEvaluator must use the deterministic fallback."""

        _ = (prompt, output_model, model_name, metadata)
        raise RuntimeError("structured feedback unavailable")


@pytest.mark.asyncio
async def test_model_backed_evaluator_uses_current_structured_client_contract() -> None:
    """Valid structured output should cross the adapter as FeedbackResult."""

    client = SuccessfulStructuredClient()
    result = await FeedbackEvaluator(
        client,
        model_name="feedback-model",
    ).evaluate(_feedback_input("谢谢，这对我很有帮助。"))

    assert isinstance(result, FeedbackResult)
    assert result.explicit_feedback == FeedbackLabel.POSITIVE
    assert result.strategy_fit == StrategyFit.GOOD
    assert "INPUT_JSON" in client.prompt
    assert "previous_intervention" in client.prompt
    assert "assistant_message" in client.prompt
    assert "next_user_message" in client.prompt


@pytest.mark.asyncio
async def test_model_backed_evaluator_without_client_uses_fake_fallback() -> None:
    """A missing model dependency should preserve deterministic behavior."""

    payload = _feedback_input("我不想继续这个方向，请换个方式。")
    expected = await FakeFeedbackEvaluator().evaluate(payload)

    result = await FeedbackEvaluator().evaluate(payload)

    assert result == expected


@pytest.mark.asyncio
async def test_model_backed_evaluator_falls_back_when_client_raises() -> None:
    """Model and structured-output failures must not escape the evaluator."""

    payload = _feedback_input("我不想继续这个方向，请换个方式。")
    expected = await FakeFeedbackEvaluator().evaluate(payload)

    result = await FeedbackEvaluator(FailingStructuredClient()).evaluate(payload)

    assert result == expected


class ScriptedDraftClient:
    """Return caller-provided data through the requested private output model."""

    def __init__(
        self,
        data: dict[str, object] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.data = data or {}
        self.error = error
        self.called = False

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        self.called = True
        _ = (prompt, model_name)
        assert metadata == {"agent": "feedback_evaluator"}
        if self.error is not None:
            raise self.error
        return output_model.model_validate(self.data)


def _draft(
    label: str,
    progress: str,
    fit: str,
    quote: str | None,
    *,
    evidence_id: str | None = "msg_feedback",
) -> dict[str, object]:
    return {
        "explicit_feedback": label,
        "objective_progress": progress,
        "strategy_fit": fit,
        "confidence": 0.9,
        "evidence_message_id": evidence_id,
        "evidence_quote": quote,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "draft", "expected"),
    [
        (
            "这个方法确实帮我理清了下一步。",
            _draft("positive", "achieved", "good", "帮我理清了下一步"),
            FeedbackLabel.POSITIVE,
        ),
        (
            "不要再分析我的情绪了，这对我没帮助。",
            _draft("negative", "not_achieved", "poor", "没帮助"),
            FeedbackLabel.NEGATIVE,
        ),
        (
            "有一点帮助，但请一次只给我一个步骤。",
            _draft("mixed", "partial", "mixed", "有一点帮助"),
            FeedbackLabel.MIXED,
        ),
        (
            "今天开会的时候，我还是很紧张。",
            _draft("absent", "unknown", "unknown", None, evidence_id=None),
            FeedbackLabel.ABSENT,
        ),
    ],
)
async def test_valid_grounded_model_drafts_are_accepted(
    text: str,
    draft: dict[str, object],
    expected: FeedbackLabel,
) -> None:
    result = await FeedbackEvaluator(ScriptedDraftClient(draft)).evaluate(
        _feedback_input(text)
    )
    assert result.explicit_feedback == expected
    assert isinstance(result, FeedbackResult)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "draft"),
    [
        ("普通继续描述。", _draft("positive", "achieved", "poor", "普通")),
        ("普通继续描述。", _draft("absent", "achieved", "unknown", None, evidence_id=None)),
        ("普通继续描述。", _draft("negative", "not_achieved", "good", "普通")),
        ("这个方法有帮助。", _draft("positive", "achieved", "good", None, evidence_id=None)),
        (
            "这个方法有帮助。",
            _draft("positive", "achieved", "good", "有帮助", evidence_id="msg_002"),
        ),
        ("这个方法有帮助。", _draft("positive", "achieved", "good", "并不存在")),
        ("普通继续描述。", _draft("absent", "unknown", "unknown", "普通")),
        ("这对我没帮助。", _draft("positive", "achieved", "good", "没帮助")),
        ("这个方法很有帮助。", _draft("negative", "not_achieved", "poor", "很有帮助")),
        (
            "有一点帮助，但请一次只给一个步骤。",
            _draft("absent", "unknown", "unknown", None, evidence_id=None),
        ),
    ],
)
async def test_invalid_semantics_or_evidence_use_fake_fallback(
    text: str,
    draft: dict[str, object],
) -> None:
    payload = _feedback_input(text)
    result = await FeedbackEvaluator(ScriptedDraftClient(draft)).evaluate(payload)
    assert result == await FakeFeedbackEvaluator().evaluate(payload)


@pytest.mark.asyncio
async def test_execution_logs_are_stable_and_private(caplog: pytest.LogCaptureFixture) -> None:
    user_text = "这个方法确实帮我理清了下一步。PRIVATE_USER_TEXT"
    quote = "帮我理清了下一步"
    with caplog.at_level(logging.INFO, logger="agents.feedback_evaluator"):
        await FeedbackEvaluator(
            ScriptedDraftClient(_draft("positive", "achieved", "good", quote)),
            model_name="feedback-model",
        ).evaluate(_feedback_input(user_text))
    record = caplog.records[-1]
    assert record.message == "model_execution"
    assert record.status == "model_success"  # type: ignore[attr-defined]
    assert record.agent == "feedback_evaluator"  # type: ignore[attr-defined]
    assert user_text not in caplog.text
    assert quote not in caplog.text


@pytest.mark.asyncio
async def test_no_client_and_client_error_log_stable_fallbacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _feedback_input("普通继续描述。")
    with caplog.at_level(logging.WARNING, logger="agents.feedback_evaluator"):
        await FeedbackEvaluator().evaluate(payload)
        await FeedbackEvaluator(
            ScriptedDraftClient(error=LLMTimeoutError("PRIVATE_ERROR"))
        ).evaluate(payload)
    assert [record.status for record in caplog.records[-2:]] == [  # type: ignore[attr-defined]
        "fallback_no_client",
        "fallback_client_error",
    ]
    assert "PRIVATE_ERROR" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evaluator",
    [
        FeedbackEvaluator(strict_model=True),
        FeedbackEvaluator(
            ScriptedDraftClient(error=LLMTimeoutError("private")),
            strict_model=True,
        ),
        FeedbackEvaluator(
            ScriptedDraftClient(_draft("positive", "achieved", "poor", "普通")),
            strict_model=True,
        ),
    ],
)
async def test_strict_model_raises_instead_of_using_fake(
    evaluator: FeedbackEvaluator,
) -> None:
    with pytest.raises(ModelExecutionFailure):
        await evaluator.evaluate(_feedback_input("普通继续描述。"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_payload",
    [
        _feedback_input("text").model_copy(
            update={
                "next_user_message": _feedback_input("text").next_user_message.model_copy(
                    update={"session_id": SessionId("other")}
                )
            }
        ),
        _feedback_input("text").model_copy(
            update={
                "assistant_message": _feedback_input("text").assistant_message.model_copy(
                    update={"id": MessageId("other")}
                )
            }
        ),
        _feedback_input("text").model_copy(
            update={
                "assistant_message": _feedback_input("text").assistant_message.model_copy(
                    update={"role": MessageRole.USER}
                )
            }
        ),
        _feedback_input("text").model_copy(
            update={
                "next_user_message": _feedback_input("text").next_user_message.model_copy(
                    update={"role": MessageRole.ASSISTANT}
                )
            }
        ),
        _feedback_input("text").model_copy(
            update={
                "next_user_message": _feedback_input("text").next_user_message.model_copy(
                    update={"sequence_number": 2}
                )
            }
        ),
        _feedback_input("text").model_copy(
            update={
                "previous_intervention": _feedback_input("text").previous_intervention.model_copy(
                    update={"status": InterventionStatus.EVALUATED}
                )
            }
        ),
    ],
)
async def test_invalid_input_relationship_raises_without_calling_client(
    invalid_payload: FeedbackInput,
) -> None:
    client = ScriptedDraftClient()
    with pytest.raises(ValueError):
        await FeedbackEvaluator(client).evaluate(invalid_payload)
    assert client.called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "I am okay, but work is still stressful.",
        "My manager was helpful today.",
        "这个同事很有帮助，我今天还是很焦虑。",
        "我可以，但是今天还很紧张。",
        "My colleague was helpful, but the project is difficult.",
        "经理今天对我很有帮助。",
    ],
)
async def test_fake_ambiguous_self_or_third_party_descriptions_are_absent(
    text: str,
) -> None:
    result = await FakeFeedbackEvaluator().evaluate(_feedback_input(text))
    assert result.explicit_feedback == FeedbackLabel.ABSENT
    assert result.objective_progress == ObjectiveProgress.UNKNOWN
    assert result.strategy_fit == StrategyFit.UNKNOWN
    assert result.recommended_adjustment is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("这个回应对我有帮助。", FeedbackLabel.POSITIVE),
        ("你刚才的方法确实帮我理清了下一步。", FeedbackLabel.POSITIVE),
        ("This response was helpful.", FeedbackLabel.POSITIVE),
        ("这对我没帮助，请换个方式。", FeedbackLabel.NEGATIVE),
        ("不要再分析我的情绪。", FeedbackLabel.NEGATIVE),
        ("This approach does not work for me.", FeedbackLabel.NEGATIVE),
        ("可以，但请一次只给我一个步骤。", FeedbackLabel.MIXED),
        ("这个回应有帮助，不过不要一次给我太多建议。", FeedbackLabel.MIXED),
        ("This response was helpful, but please give one step at a time.", FeedbackLabel.MIXED),
    ],
)
async def test_fake_keeps_high_precision_explicit_feedback(
    text: str,
    label: FeedbackLabel,
) -> None:
    assert (
        await FakeFeedbackEvaluator().evaluate(_feedback_input(text))
    ).explicit_feedback == label


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "I am okay, but work is still stressful.",
        "My manager was helpful today.",
        "这个同事很有帮助，我今天还是很焦虑。",
        "我可以，但是今天还很紧张。",
    ],
)
async def test_valid_model_absent_for_ambiguous_text_is_accepted(
    text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    draft = _draft("absent", "unknown", "unknown", None, evidence_id=None)
    with caplog.at_level(logging.INFO, logger="agents.feedback_evaluator"):
        result = await FeedbackEvaluator(
            ScriptedDraftClient(draft), strict_model=True
        ).evaluate(_feedback_input(text))
    assert result.explicit_feedback == FeedbackLabel.ABSENT
    assert caplog.records[-1].status == "model_success"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_model_cannot_supply_free_form_recommended_adjustment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unsafe_text = "diagnose bipolar disorder"
    draft = _draft("positive", "achieved", "good", "这个回应有帮助")
    draft["recommended_adjustment"] = unsafe_text
    with caplog.at_level(logging.WARNING, logger="agents.feedback_evaluator"):
        with pytest.raises(ModelExecutionFailure) as captured:
            await FeedbackEvaluator(
                ScriptedDraftClient(draft), strict_model=True
            ).evaluate(_feedback_input("这个回应有帮助。"))
    assert captured.value.status.value == "fallback_schema_validation_error"
    assert unsafe_text not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "draft", "expected_adjustment"),
    [
        ("这个回应有帮助。", _draft("positive", "achieved", "good", "有帮助"), None),
        ("继续说工作。", _draft("absent", "unknown", "unknown", None, evidence_id=None), None),
        (
            "这对我没帮助。",
            _draft("negative", "not_achieved", "poor", "没帮助"),
            "switch_or_clarify_strategy",
        ),
        (
            "这个回应有帮助，请一次只给我一个步骤。",
            _draft("mixed", "partial", "mixed", "这个回应有帮助"),
            "adjust_format_or_support_style",
        ),
    ],
)
async def test_model_results_use_deterministic_adjustment_mapping(
    text: str,
    draft: dict[str, object],
    expected_adjustment: str | None,
) -> None:
    result = await FeedbackEvaluator(ScriptedDraftClient(draft)).evaluate(
        _feedback_input(text)
    )
    assert result.recommended_adjustment == expected_adjustment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_adjustment"),
    [
        ("这个回应有帮助。", None),
        ("继续说工作。", None),
        ("这对我没帮助。", "switch_or_clarify_strategy"),
        (
            "这个回应有帮助，不过不要一次给我太多建议。",
            "adjust_format_or_support_style",
        ),
    ],
)
async def test_fake_results_use_same_deterministic_adjustment_mapping(
    text: str,
    expected_adjustment: str | None,
) -> None:
    result = await FakeFeedbackEvaluator().evaluate(_feedback_input(text))
    assert result.recommended_adjustment == expected_adjustment
