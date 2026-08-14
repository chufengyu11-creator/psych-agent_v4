"""Strategy planner implementations."""

from pathlib import Path

from llm.structured_client import StructuredLLMClientProtocol
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
                objective="align with the support style the user wants",
                reason="previous strategy was rejected or fit poorly",
                avoid=["repeat the previous strategy"],
                expected_signals=["user clarifies preferred support style"],
                switch_conditions=["risk appears", "user asks for action planning"],
            )
        if payload.session_state.user_preferences:
            return StrategyPlan(
                conversation_phase=ConversationPhase.INTERVENTION,
                primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
                objective="find one manageable next step with the user",
                reason="user has expressed a preference for concrete suggestions",
                avoid=["give too many suggestions", "decide for the user"],
                expected_signals=["user can choose one small action"],
                switch_conditions=[
                    "user rejects action suggestions",
                    "emotion escalates",
                    "risk appears",
                ],
            )
        return StrategyPlan(
            conversation_phase=ConversationPhase.EXPLORATION,
            primary_strategy=StrategyType.REFLECTIVE_LISTENING,
            objective="receive the user expression and clarify the current difficulty",
            reason="start with supportive listening when action preference is unclear",
            avoid=["diagnosis", "medication advice", "premature conclusions"],
            expected_signals=["user shares more context"],
            switch_conditions=["user asks for advice", "risk appears"],
        )


class StrategyPlanner:
    """Model-backed planner that selects the next typed dialogue strategy."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol,
        *,
        model_name: str | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a strategy planner with an injectable structured LLM client."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "You choose the next counselling-support dialogue strategy. "
                "Return a StrategyPlan JSON object and do not write the final user-facing reply."
            ),
        )

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
        return _enforce_feedback_switch(payload, plan)

    def _build_prompt(self, payload: StrategyPlannerInput) -> str:
        """Render the planning prompt from the typed payload."""

        feedback = payload.feedback.model_dump_json() if payload.feedback is not None else "null"
        return (
            f"{self._prompt_template}\n\n"
            "Important rules:\n"
            "- Choose strategy only; do not generate the assistant reply.\n"
            "- Avoid diagnosis, medication advice, and unsupported claims.\n"
            "- If risk route is not normal_dialogue, prefer safety_check.\n\n"
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


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when it is absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _fallback_plan(payload: StrategyPlannerInput) -> StrategyPlan:
    """Return a deterministic safe plan if model planning fails."""

    if payload.risk.route != RiskRoute.NORMAL:
        return StrategyPlan(
            conversation_phase=ConversationPhase.SAFETY_CHECK,
            primary_strategy=StrategyType.SAFETY_CHECK,
            objective="prioritize checking and handling the current safety risk",
            reason="risk route is not normal dialogue, so choose a conservative safety strategy",
            avoid=[
                "continue ordinary dialogue",
                "ignore safety signals",
                "diagnosis",
                "medication advice",
            ],
            expected_signals=["user safety status is clarified"],
            switch_conditions=["risk clears", "risk escalates"],
        )
    if _has_poor_negative_feedback(payload):
        return StrategyPlan(
            conversation_phase=ConversationPhase.INTERVENTION,
            primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
            objective="switch to concrete collaborative problem solving after user feedback",
            reason="previous strategy was explicitly rejected or fit poorly",
            avoid=[
                "repeat previous strategy",
                "over-analyze emotion",
                "diagnosis",
                "medication advice",
            ],
            expected_signals=["user can choose one small next step"],
            switch_conditions=[
                "user rejects concrete suggestions",
                "risk escalates",
                "user wants to end",
            ],
        )
    if payload.session_state.user_preferences or payload.memories.interaction_preferences:
        return StrategyPlan(
            conversation_phase=ConversationPhase.INTERVENTION,
            primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
            objective="follow the user's preference and find one manageable next step",
            reason="state or memory indicates preference for concrete low-pressure support",
            avoid=[
                "give too many suggestions",
                "decide for the user",
                "diagnosis",
                "medication advice",
            ],
            expected_signals=["user can confirm or adjust one small action"],
            switch_conditions=["user rejects action advice", "emotion escalates", "risk appears"],
        )
    return StrategyPlan(
        conversation_phase=ConversationPhase.EXPLORATION,
        primary_strategy=StrategyType.REFLECTIVE_LISTENING,
        objective="receive the user's expression and help clarify the current difficulty",
        reason="no clear action preference or negative feedback is present",
        avoid=["diagnosis", "medication advice", "premature conclusions"],
        expected_signals=["user shares more context"],
        switch_conditions=[
            "user asks for advice",
            "risk appears",
            "user says the strategy does not fit",
        ],
    )


def _enforce_feedback_switch(
    payload: StrategyPlannerInput,
    plan: StrategyPlan,
) -> StrategyPlan:
    """Avoid repeating reflective listening after explicit poor feedback."""

    if not _has_poor_negative_feedback(payload):
        return plan
    if plan.primary_strategy != StrategyType.REFLECTIVE_LISTENING:
        return plan
    return StrategyPlan(
        conversation_phase=ConversationPhase.INTERVENTION,
        primary_strategy=StrategyType.COLLABORATIVE_PROBLEM_SOLVING,
        objective=plan.objective or "switch to more concrete collaborative problem solving",
        reason=f"{plan.reason}; avoided repeating reflective_listening after negative feedback",
        avoid=sorted(set(plan.avoid + ["repeat previous strategy", "over-analyze emotion"])),
        expected_signals=plan.expected_signals or ["user can choose one small next step"],
        switch_conditions=plan.switch_conditions
        or ["user rejects concrete suggestions", "risk appears"],
    )


def _has_poor_negative_feedback(payload: StrategyPlannerInput) -> bool:
    """Return whether prior intervention feedback requires a strategy switch."""

    return (
        payload.feedback is not None
        and payload.feedback.explicit_feedback == FeedbackLabel.NEGATIVE
        and payload.feedback.strategy_fit == StrategyFit.POOR
    )