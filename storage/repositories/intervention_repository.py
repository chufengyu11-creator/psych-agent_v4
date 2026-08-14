"""Intervention repository protocol, in-memory store, and SQLAlchemy implementation."""

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.common import InterventionId, MessageId, SessionId, UserId
from schemas.feedback import FeedbackResult
from schemas.intervention import InterventionRecord, InterventionStatus
from storage.models.intervention import (
    INTERVENTION_STATUS_PENDING,
    InterventionEventModel,
)
from storage.models.message import MessageModel
from storage.models.session import SessionModel
from storage.repositories.errors import SessionNotFoundError


class InterventionRepository(Protocol):
    """Persistence boundary for pending and evaluated interventions."""

    async def list_for_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> list[InterventionRecord]:
        """Return all intervention records for a session in creation order."""

    async def get_pending(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> InterventionRecord | None:
        """Return the active pending intervention for a session."""

    async def create_pending(
        self,
        user_id: UserId,
        session_id: SessionId,
        assistant_message_id: MessageId,
        strategy: str,
        objective: str,
        expected_signals: list[str],
    ) -> InterventionRecord:
        """Create the intervention that will be evaluated next turn."""

    async def complete(
        self,
        user_id: UserId,
        session_id: SessionId,
        intervention_id: InterventionId,
        feedback: FeedbackResult,
    ) -> None:
        """Mark an intervention evaluated and attach feedback fields."""


class InMemoryInterventionRepository:
    """Volatile intervention repository for fake adaptive-loop testing."""

    def __init__(self) -> None:
        """Create an empty intervention store."""

        self._records: dict[InterventionId, InterventionRecord] = {}
        self._owners: dict[InterventionId, UserId] = {}

    async def list_for_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> list[InterventionRecord]:
        """Return all intervention records for the session."""

        return [
            record
            for intervention_id, record in self._records.items()
            if self._owners[intervention_id] == user_id
            and record.session_id == session_id
        ]

    async def get_pending(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> InterventionRecord | None:
        """Return the first pending intervention for the session."""

        for intervention_id, record in self._records.items():
            if (
                self._owners[intervention_id] == user_id
                and record.session_id == session_id
                and record.status == InterventionStatus.PENDING
            ):
                return record
        return None

    async def create_pending(
        self,
        user_id: UserId,
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
        self._owners[record.intervention_id] = user_id
        return record

    async def complete(
        self,
        user_id: UserId,
        session_id: SessionId,
        intervention_id: InterventionId,
        feedback: FeedbackResult,
    ) -> None:
        """Store evaluation results on an existing pending intervention."""

        record = self._records[intervention_id]
        if self._owners[intervention_id] != user_id or record.session_id != session_id:
            raise KeyError(str(intervention_id))
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


class SqlAlchemyInterventionRepository:
    """SQLAlchemy persistence for intervention lifecycle records."""

    def __init__(self, session: AsyncSession) -> None:
        """Use the caller-owned asynchronous session."""

        self._session = session

    async def list_for_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> list[InterventionRecord]:
        """Return all intervention lifecycle rows for a session."""

        statement = (
            select(InterventionEventModel)
            .join(
                MessageModel,
                InterventionEventModel.assistant_message_id == MessageModel.id,
            )
            .join(
                SessionModel,
                InterventionEventModel.session_pk == SessionModel.id,
            )
            .where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
            .order_by(MessageModel.sequence_number)
        )
        result = await self._session.execute(statement)
        return [self._to_schema(row, session_id) for row in result.scalars().all()]

    async def get_pending(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> InterventionRecord | None:
        """Return the active pending intervention for a session."""

        statement = (
            select(InterventionEventModel)
            .join(
                SessionModel,
                InterventionEventModel.session_pk == SessionModel.id,
            )
            .where(
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
                InterventionEventModel.status == INTERVENTION_STATUS_PENDING,
            )
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return self._to_schema(row, session_id)

    async def create_pending(
        self,
        user_id: UserId,
        session_id: SessionId,
        assistant_message_id: MessageId,
        strategy: str,
        objective: str,
        expected_signals: list[str],
    ) -> InterventionRecord:
        """Create and flush one pending intervention record."""

        session_row = await self._get_owned_session(user_id, session_id)
        if session_row is None:
            raise SessionNotFoundError(f"Session {str(session_id)!r} does not exist.")
        row = InterventionEventModel(
            id=f"int_{uuid4().hex}",
            session_pk=session_row.id,
            assistant_message_id=str(assistant_message_id),
            strategy=strategy,
            objective=objective,
            expected_signals=expected_signals,
            status=InterventionStatus.PENDING.value,
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_schema(row, session_id)

    async def complete(
        self,
        user_id: UserId,
        session_id: SessionId,
        intervention_id: InterventionId,
        feedback: FeedbackResult,
    ) -> None:
        """Mark a pending intervention as evaluated and flush the update."""

        statement = (
            select(InterventionEventModel)
            .join(
                SessionModel,
                InterventionEventModel.session_pk == SessionModel.id,
            )
            .where(
                InterventionEventModel.id == str(intervention_id),
                SessionModel.user_id == str(user_id),
                SessionModel.session_id == str(session_id),
            )
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(str(intervention_id))
        row.status = InterventionStatus.EVALUATED.value
        row.observed_response = feedback.observed_response
        row.explicit_feedback = feedback.explicit_feedback.value
        row.strategy_fit = feedback.strategy_fit.value
        row.objective_progress = feedback.objective_progress.value
        row.recommended_adjustment = feedback.recommended_adjustment
        row.feedback_confidence = feedback.confidence
        row.evaluated_at = datetime.now(UTC)
        await self._session.flush()

    async def _get_owned_session(
        self,
        user_id: UserId,
        session_id: SessionId,
    ) -> SessionModel | None:
        """Resolve a session only through its owner-scoped external ID."""

        statement = select(SessionModel).where(
            SessionModel.user_id == str(user_id),
            SessionModel.session_id == str(session_id),
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    def _to_schema(
        self,
        row: InterventionEventModel,
        session_id: SessionId,
    ) -> InterventionRecord:
        """Convert one ORM row into the public intervention contract."""

        return InterventionRecord(
            intervention_id=InterventionId(row.id),
            session_id=session_id,
            assistant_message_id=MessageId(row.assistant_message_id),
            strategy=row.strategy,
            objective=row.objective,
            expected_signals=row.expected_signals,
            status=InterventionStatus(row.status),
            observed_response=row.observed_response,
            explicit_feedback=row.explicit_feedback,
            strategy_fit=row.strategy_fit,
            objective_progress=row.objective_progress,
            recommended_adjustment=row.recommended_adjustment,
        )


__all__ = [
    "InMemoryInterventionRepository",
    "InterventionRepository",
    "SqlAlchemyInterventionRepository",
]
