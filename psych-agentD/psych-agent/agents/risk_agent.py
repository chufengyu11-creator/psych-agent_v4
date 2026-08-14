"""Risk agent implementations."""

from pathlib import Path

from pydantic import ValidationError

from llm.exceptions import (
    LLMError,
    LLMHTTPError,
    LLMRetryExhaustedError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)
from llm.structured_client import StructuredLLMClientProtocol
from schemas.risk import RiskInput, RiskLevel, RiskResult, RiskRoute

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "risk_agent.md"

_SELF_HARM_TERMS = (
    "\u81ea\u6740",
    "\u4f24\u5bb3\u81ea\u5df1",
    "\u7ed3\u675f\u751f\u547d",
    "\u4e0d\u60f3\u6d3b",
    "suicide",
    "kill myself",
    "end my life",
    "hurt myself",
)
_HARM_OTHERS_TERMS = (
    "\u6740\u4e86\u4ed6",
    "\u6740\u4e86\u5979",
    "\u4f24\u5bb3\u522b\u4eba",
    "\u4f24\u5bb3\u4ed6\u4eec",
    "\u62a5\u590d\u4ed6\u4eec",
    "kill him",
    "kill her",
    "kill them",
    "hurt someone",
    "hurt them",
)
_PLAN_TERMS = (
    "\u8ba1\u5212",
    "\u51c6\u5907",
    "\u4eca\u665a",
    "\u4eca\u5929",
    "\u9a6c\u4e0a",
    "\u5200",
    "\u836f",
    "\u7ef3",
    "plan",
    "tonight",
    "today",
    "now",
    "knife",
    "pills",
    "gun",
)


class FakeRiskAgent:
    """Rule-based risk analyzer used before the real model is available."""

    async def run(self, payload: RiskInput) -> RiskResult:
        """Alias for analyze so the class satisfies the generic Agent protocol."""

        return await self.analyze(payload)

    async def analyze(self, payload: RiskInput) -> RiskResult:
        """Return a conservative structured risk result for one message."""

        text = payload.current_message.content.lower()
        crisis_terms = [
            "\u81ea\u6740",
            "\u4f24\u5bb3\u81ea\u5df1",
            "suicide",
            "kill myself",
            "end my life",
        ]
        if any(term in text for term in crisis_terms):
            return RiskResult(
                risk_level=RiskLevel.HIGH,
                categories=["self_harm"],
                needs_clarification=True,
                route=RiskRoute.CRISIS,
                reason_codes=["crisis_keyword_match"],
                confidence=0.7,
            )
        return RiskResult(
            risk_level=payload.current_risk_level,
            categories=[],
            needs_clarification=False,
            route=RiskRoute.NORMAL,
            reason_codes=[],
            confidence=0.6,
        )


class RiskAgent:
    """Model-backed risk analyzer using structured LLM output."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a risk agent with an injectable structured LLM client."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "You are a risk-screening subagent for a psychological support dialogue. "
                "Classify immediate safety risk conservatively and return the RiskResult JSON."
            ),
        )

    async def run(self, payload: RiskInput) -> RiskResult:
        """Alias for analyze so the class satisfies the generic Agent protocol."""

        return await self.analyze(payload)

    async def analyze(self, payload: RiskInput) -> RiskResult:
        """Return a typed risk result without mutating input state."""

        try:
            result = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                RiskResult,
                model_name=self._model_name,
                metadata={"agent": "risk_agent"},
            )
        except Exception as exc:
            return self._fallback_result(payload, exc)
        return self._enforce_safety_floor(payload, result)

    def _build_prompt(self, payload: RiskInput) -> str:
        """Render the risk-screening prompt from the typed payload."""

        recent_messages = "\n".join(
            f"- {message.role.value} #{message.sequence_number}: {message.content}"
            for message in payload.recent_messages
        )
        if not recent_messages:
            recent_messages = "- none"

        return (
            f"{self._prompt_template}\n\n"
            "Allowed risk_level values: low, medium, high, imminent.\n"
            "Allowed route values: normal_dialogue, safety_clarification, "
            "crisis_protocol, human_escalation.\n\n"
            "## Runtime Input\n"
            f"current_risk_level: {payload.current_risk_level.value}\n"
            "current_message:\n"
            f"{payload.current_message.content}\n\n"
            "recent_messages:\n"
            f"{recent_messages}\n"
        )

    def _fallback_result(self, payload: RiskInput, error: Exception) -> RiskResult:
        """Return a conservative result if the model call fails."""

        reason = _safe_failure_reason(error)
        categories = _detected_categories(payload)
        if categories:
            return RiskResult(
                risk_level=RiskLevel.HIGH,
                categories=categories,
                needs_clarification=True,
                route=RiskRoute.CRISIS,
                reason_codes=[reason, "safety_keyword_floor"],
                confidence=0.5,
            )
        return RiskResult(
            risk_level=RiskLevel.MEDIUM,
            categories=[],
            needs_clarification=True,
            route=RiskRoute.CLARIFICATION,
            reason_codes=[reason, "conservative_fallback"],
            confidence=0.3,
        )

    def _enforce_safety_floor(self, payload: RiskInput, result: RiskResult) -> RiskResult:
        """Escalate if keyword rules detect safety risk the model missed."""

        categories = _detected_categories(payload)
        if not categories or result.route != RiskRoute.NORMAL:
            return result
        return RiskResult(
            risk_level=RiskLevel.HIGH,
            categories=sorted(set(result.categories + categories)),
            needs_clarification=True,
            route=RiskRoute.CRISIS,
            reason_codes=sorted(set(result.reason_codes + ["safety_keyword_floor"])),
            confidence=max(result.confidence, 0.6),
        )


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when it is absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _detected_categories(payload: RiskInput) -> list[str]:
    """Detect high-signal safety categories using a minimal keyword floor."""

    text = " ".join(
        [payload.current_message.content]
        + [message.content for message in payload.recent_messages]
    ).lower()
    categories: list[str] = []
    self_harm = any(term in text for term in _SELF_HARM_TERMS)
    harm_others = any(term in text for term in _HARM_OTHERS_TERMS)
    plan_signal = any(term in text for term in _PLAN_TERMS)

    if self_harm:
        categories.append("self_harm")
    if harm_others:
        categories.append("harm_others")
    if plan_signal and categories:
        categories.append("plan_or_means")
    return categories


def _safe_failure_reason(error: Exception) -> str:
    """Convert known LLM failures into stable reason codes."""

    if isinstance(error, LLMTimeoutError):
        return "llm_timeout"
    if isinstance(error, LLMStructuredOutputError):
        return "llm_structured_output_error"
    if isinstance(error, LLMRetryExhaustedError):
        return f"llm_retry_exhausted_{_safe_failure_reason(error.last_error)}"
    if isinstance(error, LLMHTTPError):
        return "llm_http_error"
    if isinstance(error, ValidationError):
        return "llm_schema_validation_error"
    if isinstance(error, LLMError):
        return "llm_error"
    return "llm_unexpected_error"