"""Output-guard implementations."""

from __future__ import annotations

import re
from pathlib import Path

from llm.structured_client import StructuredLLMClientProtocol
from schemas.risk import RiskRoute
from schemas.safety import GuardDecision, GuardInput, GuardResult

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "output_guard.md"
_FIXED_SAFE_RESPONSE = "抱歉，这条回复没有通过安全检查。我们可以先回到当下最重要的安全和支持需求。"

_RULE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "unsupported_diagnosis",
        (
            "你就是抑郁症",
            "你是抑郁症",
            "诊断你是",
            "可以诊断为",
            "临床诊断",
            "确诊",
            "you are depressed",
        ),
    ),
    (
        "medication_advice",
        (
            "药物剂量",
            "药量",
            "加到两倍",
            "加倍服药",
            "停药",
            "减药",
            "换药",
            "antidepressant dose",
        ),
    ),
    (
        "guaranteed_outcome",
        (
            "保证治好",
            "保证这样做一定能治好",
            "一定能治好",
            "肯定会好",
            "guarantee this will cure",
        ),
    ),
    (
        "ai_dependency",
        (
            "只有我真正理解你",
            "只需要和我说",
            "不需要再和其他人说",
            "别再找别人",
            "只有我能帮你",
        ),
    ),
    (
        "isolate_from_support",
        (
            "远离你的家人",
            "疏远朋友",
            "不要告诉朋友",
            "不要告诉家人",
            "别联系现实中的人",
        ),
    ),
    (
        "unsafe_action_suggestion",
        (
            "去报复",
            "伤害他",
            "伤害她",
            "伤害他们",
            "惩罚自己",
            "立刻辞职",
            "不要睡觉",
        ),
    ),
    (
        "overcertain_psychological_interpretation",
        (
            "你的潜意识就是",
            "你内心真正想要的就是",
            "根本原因一定是",
            "这说明你一定",
        ),
    ),
    (
        "prompt_or_internal_state_leak",
        (
            "system prompt",
            "系统 prompt",
            "系统提示词",
            "开发者指令",
            "内部状态",
            "system_policy",
            "hidden instruction",
        ),
    ),
)

_MEDICATION_REGEX = re.compile(
    r"(?:mg|毫克|片|粒).{0,8}(?:加|减|停|换)|(?:加|减|停|换).{0,8}(?:mg|毫克|片|粒)",
    re.IGNORECASE,
)
_SAFETY_MARKERS = ("安全", "伤害自己", "伤害他人", "立即风险", "危机", "紧急", "求助")


class FakeOutputGuard:
    """Deterministic rule-only guard for tests and offline development."""

    async def run(self, payload: GuardInput) -> GuardResult:
        """Alias for review so the class satisfies the generic Agent protocol."""

        return await self.review(payload)

    async def review(self, payload: GuardInput) -> GuardResult:
        """Apply the same deterministic pre-checks used by the model-backed guard."""

        rule_result = _rule_review(payload)
        return rule_result or GuardResult(decision=GuardDecision.ALLOW)


class OutputGuard:
    """Final rule-backed and model-backed safety review before sending a reply."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a guard with an injectable structured-output model client."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "Review the draft for safety-policy violations. Return GuardResult JSON only. "
                "When uncertain, block or route to safety rather than allow."
            ),
        )

    async def run(self, payload: GuardInput) -> GuardResult:
        """Alias for review so the class satisfies the generic Agent protocol."""

        return await self.review(payload)

    async def review(self, payload: GuardInput) -> GuardResult:
        """Return a conservative typed guard decision without modifying the input."""

        rule_result = _rule_review(payload)
        if rule_result is not None:
            return rule_result

        try:
            result = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                GuardResult,
                model_name=self._model_name,
                metadata={"agent": "output_guard"},
            )
        except Exception:
            return _guard_failure()
        return _normalize_model_result(result)

    def _build_prompt(self, payload: GuardInput) -> str:
        """Render the complete typed guard input for semantic model review."""

        return (
            f"{self._prompt_template}\n\n"
            "## Runtime Input\n"
            "draft_json:\n"
            f"{payload.draft.model_dump_json()}\n\n"
            "risk_json:\n"
            f"{payload.risk.model_dump_json()}\n\n"
            "context_json:\n"
            f"{payload.context.model_dump_json()}\n"
        )


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _rule_review(payload: GuardInput) -> GuardResult | None:
    """Apply non-bypassable deterministic safety and memory checks."""

    text = payload.draft.text
    violations = _rule_violations(text)
    violations.extend(_memory_violations(payload))

    if payload.risk.route != RiskRoute.NORMAL and not _contains_safety_marker(text):
        return GuardResult(
            decision=GuardDecision.ROUTE_TO_SAFETY,
            violations=["ordinary_dialogue_during_safety_route"],
        )

    if violations:
        return GuardResult(
            decision=GuardDecision.BLOCK,
            violations=sorted(set(violations)),
            rewritten_response=_FIXED_SAFE_RESPONSE,
        )
    return None


def _rule_violations(text: str) -> list[str]:
    """Return stable violation codes for high-signal unsafe language."""

    lower_text = text.lower()
    violations = [
        code
        for code, patterns in _RULE_PATTERNS
        if any(pattern.lower() in lower_text for pattern in patterns)
    ]
    if _MEDICATION_REGEX.search(text):
        violations.append("medication_advice")
    return violations


def _memory_violations(payload: GuardInput) -> list[str]:
    """Block references to memory IDs that were not supplied in the context."""

    known_ids = {
        str(memory.id)
        for memory in (
            payload.context.memories.semantic_memories
            + payload.context.memories.episodic_memories
        )
    }
    unknown_ids = [
        memory_id
        for memory_id in payload.draft.referenced_memory_ids
        if memory_id not in known_ids
    ]
    return ["inappropriate_memory_use"] if unknown_ids else []


def _contains_safety_marker(text: str) -> bool:
    """Return whether a draft explicitly addresses current safety."""

    return any(marker in text for marker in _SAFETY_MARKERS)


def _normalize_model_result(result: GuardResult) -> GuardResult:
    """Repair incomplete model decisions conservatively."""

    if result.decision == GuardDecision.ALLOW:
        return result
    if result.decision == GuardDecision.REWRITE and not result.rewritten_response:
        return GuardResult(
            decision=GuardDecision.BLOCK,
            violations=sorted(set(result.violations + ["missing_rewrite_text"])),
            rewritten_response=_FIXED_SAFE_RESPONSE,
        )
    if result.decision == GuardDecision.BLOCK and not result.violations:
        return GuardResult(
            decision=GuardDecision.BLOCK,
            violations=["model_blocked"],
            rewritten_response=result.rewritten_response or _FIXED_SAFE_RESPONSE,
        )
    return result


def _guard_failure() -> GuardResult:
    """Fail closed when semantic review is unavailable or invalid."""

    return GuardResult(
        decision=GuardDecision.BLOCK,
        violations=["output_guard_failure"],
        rewritten_response=_FIXED_SAFE_RESPONSE,
    )
