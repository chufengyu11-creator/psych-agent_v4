"""Strategy planner implementations."""

from __future__ import annotations

from pathlib import Path

from llm.client import LLMClient
from schemas.feedback import FeedbackLabel, StrategyFit
from schemas.risk import RiskRoute
from schemas.state import ConversationPhase
from schemas.strategy import StrategyPlan, StrategyPlannerInput, StrategyType

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "strategy_planner.md"


class FakeStrategyPlanner:
    """Deterministic planner used to test the orchestration contract."""

    async def run(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Alias for plan so the class satisfies Agent protocol."""

        return await self.plan(payload)

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Choose a simple strategy based on feedback and user preferences."""

        if (
            payload.feedback is not None
            and payload.feedback.explicit_feedback == FeedbackLabel.NEGATIVE
            and payload.feedback.strategy_fit == StrategyFit.POOR
        ):
            return StrategyPlan(
                conversation_phase=ConversationPhase.GOAL_ALIGNMENT,
                primary_strategy=StrategyType.CLARIFICATION,
                objective="先校准用户希望得到的支持方式",
                reason="上一轮策略被用户明确拒绝或效果较差",
                avoid=["继续重复上一轮策略"],
                expected_signals=["用户说明更偏好的帮助方式"],
                switch_conditions=["出现安全风险", "用户要求具体行动计划"],
            )
        if payload.session_state.user_preferences:
            return StrategyPlan(
                conversation_phase=ConversationPhase.INTERVENTION,
                primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
                objective="和用户一起找到一个可承受的小步骤",
                reason="用户表达了希望获得具体建议的偏好",
                avoid=["一次性给太多建议", "替用户做决定"],
                expected_signals=["用户能选择一个可尝试的小行动"],
                switch_conditions=["用户拒绝行动建议", "情绪明显升级", "出现安全风险"],
            )
        return StrategyPlan(
            conversation_phase=ConversationPhase.EXPLORATION,
            primary_strategy=StrategyType.REFLECTIVE_LISTENING,
            objective="承接用户表达并帮助其继续澄清当前困扰",
            reason="缺少明确行动偏好时先以支持性倾听开始",
            avoid=["诊断", "药物建议", "过早给结论"],
            expected_signals=["用户愿意补充更多情境"],
            switch_conditions=["用户要求建议", "出现安全风险"],
        )


class StrategyPlanner:
    """Model-backed planner that selects the next typed dialogue strategy."""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _PROMPT_PATH.read_text(encoding="utf-8")

    async def run(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Alias for plan so the class satisfies Agent protocol."""

        return await self.plan(payload)

    async def plan(self, payload: StrategyPlannerInput) -> StrategyPlan:
        """Choose a StrategyPlan without generating the assistant response."""

        try:
            plan = await self._llm_client.generate_structured(
                self._build_prompt(payload),
                StrategyPlan,
                model_name=self._model_name,
                metadata={"agent": "strategy_planner"},
            )
        except Exception:
            return _fallback_plan(payload)

        if not isinstance(plan, StrategyPlan):
            return _fallback_plan(payload)
        return _enforce_feedback_switch(payload, plan)

    def _build_prompt(self, payload: StrategyPlannerInput) -> str:
        feedback = payload.feedback.model_dump_json() if payload.feedback is not None else "null"
        return (
            f"{self._prompt_template}\n\n"
            "## Runtime Input\n"
            "session_state_json:\n"
            f"{payload.session_state.model_dump_json()}\n\n"
            "risk_json:\n"
            f"{payload.risk.model_dump_json()}\n\n"
            "feedback_json:\n"
            f"{feedback}\n\n"
            "retrieved_memories_json:\n"
            f"{payload.memories.model_dump_json()}\n"
        )


def _fallback_plan(payload: StrategyPlannerInput) -> StrategyPlan:
    if payload.risk.route != RiskRoute.NORMAL:
        return StrategyPlan(
            conversation_phase=ConversationPhase.SAFETY_CHECK,
            primary_strategy=StrategyType.SAFETY_CHECK,
            objective="优先确认并处理当前安全风险",
            reason="风险路由不是普通对话，采用保守安全策略",
            avoid=["普通对话推进", "忽视安全信号", "诊断", "药物建议"],
            expected_signals=["用户安全状态被进一步澄清"],
            switch_conditions=["风险解除后回到支持性对话", "风险升级时进入危机流程"],
        )
    if _has_poor_negative_feedback(payload):
        return StrategyPlan(
            conversation_phase=ConversationPhase.INTERVENTION,
            primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
            objective="根据用户反馈切换到具体、共同解决问题的方式",
            reason="上一轮策略被用户明确拒绝或效果较差，需要使用不同策略",
            avoid=["继续重复上一轮策略", "继续过度分析情绪", "诊断", "药物建议"],
            expected_signals=["用户能选择一个可尝试的小步骤"],
            switch_conditions=["用户仍拒绝具体建议", "出现安全风险", "用户要求结束会话"],
        )
    if payload.session_state.user_preferences or payload.memories.interaction_preferences:
        return StrategyPlan(
            conversation_phase=ConversationPhase.INTERVENTION,
            primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
            objective="按用户偏好协作找到一个可承受的小步骤",
            reason="当前状态或记忆显示用户偏好具体、低压力的支持方式",
            avoid=["一次性给太多建议", "替用户做决定", "诊断", "药物建议"],
            expected_signals=["用户能确认或调整一个小行动"],
            switch_conditions=["用户拒绝行动建议", "情绪明显升级", "出现安全风险"],
        )
    return StrategyPlan(
        conversation_phase=ConversationPhase.EXPLORATION,
        primary_strategy=StrategyType.REFLECTIVE_LISTENING,
        objective="承接用户表达并帮助其继续澄清当前困扰",
        reason="没有明确行动偏好或负反馈时，先使用支持性倾听",
        avoid=["诊断", "药物建议", "过早给结论"],
        expected_signals=["用户愿意补充更多情境"],
        switch_conditions=["用户要求建议", "出现安全风险", "用户表达策略不适配"],
    )


def _enforce_feedback_switch(
    payload: StrategyPlannerInput,
    plan: StrategyPlan,
) -> StrategyPlan:
    if not _has_poor_negative_feedback(payload):
        return plan
    if plan.primary_strategy != StrategyType.REFLECTIVE_LISTENING:
        return plan
    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        objective=plan.objective or "根据用户反馈切换到更具体的协作解决方式",
        reason=f"{plan.reason}; 已根据负反馈避免继续 reflective_listening",
        avoid=sorted(set(plan.avoid + ["继续重复上一轮策略", "继续过度分析情绪"])),
        expected_signals=plan.expected_signals or ["用户能选择一个可尝试的小步骤"],
        switch_conditions=plan.switch_conditions or ["用户仍拒绝具体建议", "出现安全风险"],
    )


def _has_poor_negative_feedback(payload: StrategyPlannerInput) -> bool:
    return (
        payload.feedback is not None
        and payload.feedback.explicit_feedback == FeedbackLabel.NEGATIVE
        and payload.feedback.strategy_fit == StrategyFit.POOR
    )
