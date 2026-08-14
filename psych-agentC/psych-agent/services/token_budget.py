"""Token budget helpers for context construction."""

DEFAULT_BUDGETS: dict[str, int] = {
    "system_policy": 1200,
    "risk_state": 300,
    "session_state": 800,
    "strategy_plan": 400,
    "rolling_summary": 1200,
    "recent_messages": 3000,
    "long_term_memory": 800,
    "knowledge": 1200,
}


class TokenBudgetManager:
    """Provides per-section token budgets to ContextBuilder."""

    def __init__(self, budgets: dict[str, int] | None = None) -> None:
        """Create a manager with optional budget overrides."""

        self._budgets = DEFAULT_BUDGETS | (budgets or {})

    def budget_for(self, section_name: str) -> int:
        """Return the configured token budget for one context section."""

        return self._budgets[section_name]
