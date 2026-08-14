"""Feedback evaluator implementations."""

import json
import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import Field

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import ContractModel, MessageId
from schemas.feedback import (
    FeedbackInput,
    FeedbackLabel,
    FeedbackResult,
    ObjectiveProgress,
    StrategyFit,
)
from schemas.intervention import InterventionStatus
from schemas.messages import MessageRole
from services.model_execution import (
    ModelExecutionEvent,
    ModelExecutionFailure,
    ModelExecutionStatus,
    log_model_execution,
    model_exception_status,
)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "feedback_evaluator.md"
_LOGGER = logging.getLogger(__name__)
_AGENT_NAME = "feedback_evaluator"

_OBSERVED_RESPONSES = {
    FeedbackLabel.POSITIVE: "用户明确表示上一轮回应有帮助",
    FeedbackLabel.NEGATIVE: "用户明确拒绝上一轮方向或表示该方式不匹配",
    FeedbackLabel.MIXED: "用户部分接受上一轮回应，同时明确要求调整支持方式",
    FeedbackLabel.ABSENT: "用户没有明确评价上一轮策略",
}

_RECOMMENDED_ADJUSTMENTS: Mapping[FeedbackLabel, str | None] = MappingProxyType(
    {
        FeedbackLabel.POSITIVE: None,
        FeedbackLabel.ABSENT: None,
        FeedbackLabel.NEGATIVE: "switch_or_clarify_strategy",
        FeedbackLabel.MIXED: "adjust_format_or_support_style",
    }
)

_NEGATIVE_TERMS = (
    "这对我没帮助",
    "这对我没有帮助",
    "这个回应没帮助",
    "这个回应没有帮助",
    "这个方法没用",
    "这个方法不管用",
    "这个方法不太行",
    "你刚才的方法没帮助",
    "你刚才的方法没用",
    "不太行",
    "没用",
    "不管用",
    "不要再分析我的情绪",
    "我不想继续分析情绪",
    "我不想继续这个方向",
    "我不想用这种方式",
    "请换个方式",
    "这不是我想要的支持方式",
    "你没有理解我的意思",
    "你没理解我的意思",
    "this was not helpful",
    "that response did not help",
    "this approach does not work for me",
    "do not continue with this approach",
    "i do not want this kind of response",
    "i do not want more reflection",
    "i don't want more reflection",
    "please switch approach",
    "try another way",
    "you misunderstood me",
)
_POSITIVE_TERMS = (
    "这个回应有帮助",
    "这个回应很有帮助",
    "这个回应对我有帮助",
    "这个方法有帮助",
    "这个方法很有帮助",
    "你刚才的建议有帮助",
    "你刚才说的对我有帮助",
    "这对我有帮助",
    "这对我很有帮助",
    "确实帮我理清了下一步",
    "这个回应正是我需要的",
    "你的建议帮到我了",
    "this response was helpful",
    "that response was helpful",
    "this approach helped me",
    "that helped me",
    "what you said helped me",
    "your suggestion was helpful",
    "this is what i needed",
)
_PARTIAL_ACCEPTANCE_TERMS = (
    "有点帮助",
    "有一点帮助",
    "部分有帮助",
    "this response helped a little",
    "that response helped a little",
)
_ADJUSTMENT_TERMS = (
    "请一次只给我一个步骤",
    "请一次只给一个步骤",
    "不要一次给我太多建议",
    "不要一次给太多建议",
    "不要一次给太多步骤",
    "请先给结论再解释",
    "能不能说得更简短",
    "请换成更具体的步骤",
    "请给我更具体的步骤",
    "请少分析情绪，多给具体方法",
    "please give one step at a time",
    "do not give too many suggestions at once",
    "could you be more concise",
    "please give the conclusion first",
    "use more concrete steps",
)


@dataclass(frozen=True, slots=True)
class _ExplicitFeedbackSignals:
    """High-precision observable signals about the previous intervention."""

    positive: bool
    negative: bool
    partial_acceptance: bool
    adjustment_request: bool


class _GroundedFeedbackDraft(ContractModel):
    """Private model output that must cite the next user message."""

    explicit_feedback: FeedbackLabel
    objective_progress: ObjectiveProgress
    strategy_fit: StrategyFit
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_message_id: MessageId | None = None
    evidence_quote: str | None = None


class FakeFeedbackEvaluator:
    """Rule-based evaluator for the adaptive-loop skeleton."""

    async def run(self, payload: FeedbackInput) -> FeedbackResult:
        """Alias for evaluate so the class satisfies Agent protocol."""

        return await self.evaluate(payload)

    async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
        """Classify only explicit, observable feedback in the latest user message."""

        signals = _extract_explicit_feedback_signals(
            payload.next_user_message.content
        )
        if (
            signals.positive or signals.partial_acceptance
        ) and signals.adjustment_request:
            return _build_feedback_result(
                explicit_feedback=FeedbackLabel.MIXED,
                objective_progress=ObjectiveProgress.PARTIAL,
                strategy_fit=StrategyFit.MIXED,
                confidence=0.75,
            )
        if signals.negative:
            return _build_feedback_result(
                explicit_feedback=FeedbackLabel.NEGATIVE,
                objective_progress=ObjectiveProgress.NOT_ACHIEVED,
                strategy_fit=StrategyFit.POOR,
                confidence=0.8,
            )
        if signals.positive:
            return _build_feedback_result(
                explicit_feedback=FeedbackLabel.POSITIVE,
                objective_progress=ObjectiveProgress.ACHIEVED,
                strategy_fit=StrategyFit.GOOD,
                confidence=0.8,
            )
        return _build_feedback_result(
            explicit_feedback=FeedbackLabel.ABSENT,
            objective_progress=ObjectiveProgress.UNKNOWN,
            strategy_fit=StrategyFit.UNKNOWN,
            confidence=0.5,
        )


class FeedbackEvaluator:
    """Model-backed evaluator with deterministic fallback behavior."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeFeedbackEvaluator | None = None,
        prompt_template: str | None = None,
        strict_model: bool = False,
    ) -> None:
        """Create an evaluator with an optional structured model dependency."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeFeedbackEvaluator()
        self._strict_model = strict_model
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Evaluate only observable user feedback about the previous assistant "
                "intervention. Return one FeedbackResult JSON object."
            ),
        )

    async def run(self, payload: FeedbackInput) -> FeedbackResult:
        """Alias for evaluate so the class satisfies Agent protocol."""

        return await self.evaluate(payload)

    async def evaluate(self, payload: FeedbackInput) -> FeedbackResult:
        """Evaluate a grounded model draft or apply the configured safe failure mode."""

        _validate_input_relationships(payload)
        if self._llm_client is None:
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=ModelExecutionStatus.FALLBACK_NO_CLIENT,
                model_name=self._model_name,
                reason_codes=("llm_client_unavailable",),
            )
            return await self._fail_or_fallback(payload, event)
        try:
            raw_draft = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                _GroundedFeedbackDraft,
                model_name=self._model_name,
                metadata={"agent": _AGENT_NAME},
            )
            draft = _GroundedFeedbackDraft.model_validate(raw_draft)
        except Exception as error:
            status, reasons = model_exception_status(error)
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=status,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            return await self._fail_or_fallback(payload, event, cause=error)

        reasons = _feedback_validation_reasons(draft, payload)
        if reasons:
            event = ModelExecutionEvent(
                agent=_AGENT_NAME,
                status=ModelExecutionStatus.FALLBACK_SEMANTIC_VALIDATION_ERROR,
                model_name=self._model_name,
                reason_codes=reasons,
            )
            return await self._fail_or_fallback(payload, event)

        event = ModelExecutionEvent(
            agent=_AGENT_NAME,
            status=ModelExecutionStatus.MODEL_SUCCESS,
            model_name=self._model_name,
        )
        log_model_execution(_LOGGER, event)
        return _draft_to_result(draft)

    async def _fail_or_fallback(
        self,
        payload: FeedbackInput,
        event: ModelExecutionEvent,
        *,
        cause: Exception | None = None,
    ) -> FeedbackResult:
        """Log one safe event, then raise in strict mode or use the fake."""

        log_model_execution(_LOGGER, event)
        if self._strict_model:
            failure = ModelExecutionFailure(event)
            if cause is not None:
                raise failure from cause
            raise failure
        return await self._fallback.evaluate(payload)

    def _build_prompt(self, payload: FeedbackInput) -> str:
        """Render all three feedback inputs as one structured JSON payload."""

        payload_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        return f"{self._prompt_template}\n\nINPUT_JSON:\n{payload_json}"


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    """Return whether normalized text contains one explicit feedback phrase."""

    return any(_contains_feedback_term(text, term) for term in terms)


def _contains_feedback_term(text: str, term: str) -> bool:
    """Match English phrases at ASCII boundaries and Chinese phrases by substring."""

    if term.isascii():
        return (
            re.search(
                rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])",
                text,
            )
            is not None
        )
    return term in text


def _validate_input_relationships(payload: FeedbackInput) -> None:
    """Validate the typed objects describe one pending adjacent interaction."""

    intervention = payload.previous_intervention
    assistant = payload.assistant_message
    user = payload.next_user_message
    if not (intervention.session_id == assistant.session_id == user.session_id):
        raise ValueError("feedback input session IDs must match")
    if intervention.assistant_message_id != assistant.id:
        raise ValueError("feedback input assistant message ID must match intervention")
    if assistant.role != MessageRole.ASSISTANT:
        raise ValueError("feedback input assistant_message must have assistant role")
    if user.role != MessageRole.USER:
        raise ValueError("feedback input next_user_message must have user role")
    if assistant.sequence_number >= user.sequence_number:
        raise ValueError("feedback input messages must be in increasing sequence order")
    if intervention.status != InterventionStatus.PENDING:
        raise ValueError("feedback input intervention must be pending")


def _feedback_validation_reasons(
    draft: _GroundedFeedbackDraft,
    payload: FeedbackInput,
) -> tuple[str, ...]:
    """Return stable reasons for invalid evidence, combinations, or contradictions."""

    reasons: list[str] = []
    allowed = {
        FeedbackLabel.ABSENT: {
            (ObjectiveProgress.UNKNOWN, StrategyFit.UNKNOWN),
        },
        FeedbackLabel.POSITIVE: {
            (ObjectiveProgress.ACHIEVED, StrategyFit.GOOD),
            (ObjectiveProgress.PARTIAL, StrategyFit.GOOD),
        },
        FeedbackLabel.NEGATIVE: {
            (ObjectiveProgress.NOT_ACHIEVED, StrategyFit.POOR),
            (ObjectiveProgress.PARTIAL, StrategyFit.POOR),
        },
        FeedbackLabel.MIXED: {
            (ObjectiveProgress.PARTIAL, StrategyFit.MIXED),
        },
    }
    if (draft.objective_progress, draft.strategy_fit) not in allowed[
        draft.explicit_feedback
    ]:
        reasons.append("invalid_feedback_field_combination")

    if draft.explicit_feedback == FeedbackLabel.ABSENT:
        if draft.evidence_message_id is not None or draft.evidence_quote is not None:
            reasons.append("unexpected_feedback_evidence")
    else:
        if draft.evidence_message_id is None or not _normalize(draft.evidence_quote or ""):
            reasons.append("missing_feedback_evidence")
        elif draft.evidence_message_id != payload.next_user_message.id:
            reasons.append("wrong_feedback_evidence_message")
        elif _normalize(draft.evidence_quote or "") not in _normalize(
            payload.next_user_message.content
        ):
            reasons.append("feedback_quote_not_found")

    reasons.extend(_contradiction_reasons(draft, payload.next_user_message.content))
    return tuple(dict.fromkeys(reasons))


def _contradiction_reasons(
    draft: _GroundedFeedbackDraft,
    user_text: str,
) -> tuple[str, ...]:
    """Reject only obvious conflicts between explicit signals and model labels."""

    signals = _extract_explicit_feedback_signals(user_text)
    quote_signals = _extract_explicit_feedback_signals(draft.evidence_quote or "")
    mixed_signal = (
        signals.positive or signals.partial_acceptance
    ) and signals.adjustment_request
    reasons: list[str] = []
    if draft.explicit_feedback == FeedbackLabel.POSITIVE and signals.negative:
        reasons.append("positive_contradicts_negative_signal")
    if (
        draft.explicit_feedback == FeedbackLabel.NEGATIVE
        and signals.positive
        and not signals.negative
    ):
        reasons.append("negative_contradicts_positive_signal")
    if draft.explicit_feedback == FeedbackLabel.ABSENT and (
        signals.positive or signals.negative
    ):
        reasons.append("absent_contradicts_explicit_signal")
    if mixed_signal and draft.explicit_feedback in {
        FeedbackLabel.POSITIVE,
        FeedbackLabel.ABSENT,
    }:
        reasons.append("mixed_feedback_missed")
    if draft.explicit_feedback == FeedbackLabel.POSITIVE and quote_signals.negative:
        reasons.append("positive_contradicts_negative_signal")
    if draft.explicit_feedback == FeedbackLabel.NEGATIVE and quote_signals.positive:
        reasons.append("negative_contradicts_positive_signal")
    return tuple(dict.fromkeys(reasons))


def _normalize(text: str) -> str:
    """Normalize Unicode and whitespace for evidence and signal comparisons."""

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _draft_to_result(draft: _GroundedFeedbackDraft) -> FeedbackResult:
    """Discard private evidence and create the unchanged public contract."""

    return _build_feedback_result(
        explicit_feedback=draft.explicit_feedback,
        objective_progress=draft.objective_progress,
        strategy_fit=draft.strategy_fit,
        confidence=draft.confidence,
    )


def _build_feedback_result(
    *,
    explicit_feedback: FeedbackLabel,
    objective_progress: ObjectiveProgress,
    strategy_fit: StrategyFit,
    confidence: float,
) -> FeedbackResult:
    """Build the public result with deterministic text and adjustment controls."""

    return FeedbackResult(
        observed_response=_OBSERVED_RESPONSES[explicit_feedback],
        explicit_feedback=explicit_feedback,
        objective_progress=objective_progress,
        strategy_fit=strategy_fit,
        recommended_adjustment=_RECOMMENDED_ADJUSTMENTS[explicit_feedback],
        confidence=confidence,
    )


def _extract_explicit_feedback_signals(text: str) -> _ExplicitFeedbackSignals:
    """Extract conservative, high-precision signals about the prior response."""

    normalized = _normalize(text)
    adjustment = _contains_any(normalized, _ADJUSTMENT_TERMS)
    partial = _contains_any(normalized, _PARTIAL_ACCEPTANCE_TERMS) or (
        normalized.startswith("可以") and adjustment
    )
    return _ExplicitFeedbackSignals(
        positive=_contains_any(normalized, _POSITIVE_TERMS),
        negative=_contains_any(normalized, _NEGATIVE_TERMS),
        partial_acceptance=partial,
        adjustment_request=adjustment,
    )


__all__ = ["FakeFeedbackEvaluator", "FeedbackEvaluator"]
