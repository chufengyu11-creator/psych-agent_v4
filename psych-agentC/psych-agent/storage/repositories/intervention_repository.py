"""Intervention repository protocol and in-memory implementation."""

from typing import Protocol

from schemas.common import InterventionId, MessageId, SessionId
from schemas.feedback import FeedbackResult
from schemas.intervention import InterventionRecord, InterventionStatus


class InterventionRepository(Protocol):
    """Persistence boundary for pending and evaluated interventions."""

    async def get_pending(self, session_id: SessionId) -> InterventionRecord | None:
        """Return the active pending intervention for a session."""

    async def create_pending(
        self,
        session_id: SessionId,
        assistant_message_id: MessageId,
        strategy: str,
        objective: str,
        expected_signals: list[str],
    ) -> InterventionRecord:
        """Create the intervention that will be evaluated next turn."""

    async def complete(self, intervention_id: InterventionId, feedback: FeedbackResult) -> None:
        """Mark an intervention evaluated and attach feedback fields."""


class InMemoryInterventionRepository:
    """Volatile intervention repository for fake adaptive-loop testing."""

    def __init__(self) -> None:
        """Create an empty intervention store."""

        self._records: dict[InterventionId, InterventionRecord] = {}

    async def get_pending(self, session_id: SessionId) -> InterventionRecord | None:
        """Return the first pending intervention for the session."""

        for record in self._records.values():
            if record.session_id == session_id and record.status == InterventionStatus.PENDING:
                return record
        return None

    async def create_pending(
        self,
        session_id: SessionId,
        assistant_message_id: MessageId,
        strategy: str,
        objective: str,
        expected_signals: list[str],
    ) -> InterventionRecord:
        """Create and store a pending intervention record."""

        record = InterventionRecord(
            intervention_id=InterventionId(f"int_{len(self._records) + 1}"),
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            strategy=strategy,
            objective=objective,
            expected_signals=expected_signals,
            status=InterventionStatus.PENDING,
        )
        self._records[record.intervention_id] = record
        return record

    async def complete(self, intervention_id: InterventionId, feedback: FeedbackResult) -> None:
        """Store evaluation results on an existing pending intervention."""

        record = self._records[intervention_id]
        self._records[intervention_id] = record.model_copy(
            update={
                "status": InterventionStatus.EVALUATED,
                "observed_response": feedback.observed_response,
                "explicit_feedback": feedback.explicit_feedback.value,
                "strategy_fit": feedback.strategy_fit.value,
                "objective_progress": feedback.objective_progress.value,
                "recommended_adjustment": feedback.recommended_adjustment,
            }
        )
