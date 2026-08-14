"""Worker for producing and persisting rolling conversation summaries."""

from typing import Protocol

from schemas.common import MessageId, UserId
from schemas.events import RollingSummaryRequestedEvent
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message, MessageRole
from schemas.state import RiskState, SessionState
from schemas.summary import RollingSummarizerInput, RollingSummary
from storage.repositories.intervention_repository import InterventionRepository
from storage.repositories.message_repository import MessageRepository
from storage.repositories.state_repository import StateRepository
from storage.repositories.summary_repository import SummaryRepository


class Summarizer(Protocol):
    """Structural contract required by :class:`SummaryWorker`."""

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Return a rolling summary for the typed payload."""


class SummaryWorker:
    """Coordinate summary inputs and persistence behind repository boundaries."""

    def __init__(
        self,
        message_repository: MessageRepository,
        state_repository: StateRepository,
        summary_repository: SummaryRepository,
        intervention_repository: InterventionRepository,
        summarizer: Summarizer,
        message_limit: int = 50,
        retain_recent_messages: int = 2,
    ) -> None:
        """Create a worker with injected persistence and summarization dependencies."""

        if message_limit < 1:
            raise ValueError("message_limit must be at least 1")
        if retain_recent_messages < 1:
            raise ValueError("retain_recent_messages must be at least 1")
        if retain_recent_messages >= message_limit:
            raise ValueError("retain_recent_messages must be smaller than message_limit")
        self._message_repository = message_repository
        self._state_repository = state_repository
        self._summary_repository = summary_repository
        self._intervention_repository = intervention_repository
        self._summarizer = summarizer
        self._message_limit = message_limit
        self._retain_recent_messages = retain_recent_messages

    async def handle_summary_requested(
        self,
        event: RollingSummaryRequestedEvent,
    ) -> RollingSummary | None:
        """Update the rolling summary through the event's requested message boundary."""

        previous_summary = await self._summary_repository.get_current(
            event.user_id,
            event.session_id,
        )
        all_messages = await self._message_repository.get_recent(
            event.user_id,
            event.session_id,
            limit=self._message_limit,
        )
        all_messages = sorted(all_messages, key=lambda message: message.sequence_number)
        messages = list(all_messages)

        if event.after_message_id is not None:
            after_message = await self._message_repository.get_by_id(
                event.user_id,
                event.after_message_id,
            )
            if after_message is not None:
                messages = [
                    message
                    for message in messages
                    if message.sequence_number <= after_message.sequence_number
                ]

        if previous_summary is not None:
            covered_to = await self._message_repository.get_by_id(
                event.user_id,
                previous_summary.covered_to,
            )
            if covered_to is not None:
                messages = [
                    message
                    for message in messages
                    if message.sequence_number > covered_to.sequence_number
                ]

        if event.after_message_id is None and event.trigger == "post_turn":
            if len(messages) <= self._retain_recent_messages:
                return previous_summary
            messages = messages[: -self._retain_recent_messages]

        if not messages:
            return previous_summary

        current_state = _filter_state_for_coverage(
            await self._state_repository.get_current(
                event.user_id,
                event.session_id,
            ),
            previous_summary=previous_summary,
            uncovered_messages=messages,
        )
        interventions = await self._filter_interventions_for_coverage(
            event.user_id,
            await self._intervention_repository.list_for_session(
                event.user_id,
                event.session_id
            ),
            all_messages=all_messages,
            covered_to_sequence=messages[-1].sequence_number,
        )
        payload = RollingSummarizerInput(
            session_id=event.session_id,
            previous_summary=previous_summary,
            uncovered_messages=messages,
            current_state=current_state,
            interventions=interventions,
        )
        summary = await self._summarizer.summarize(payload)
        await self._summary_repository.save_version(event.user_id, summary)
        return summary

    async def _filter_interventions_for_coverage(
        self,
        user_id: UserId,
        interventions: list[InterventionRecord],
        *,
        all_messages: list[Message],
        covered_to_sequence: int,
    ) -> list[InterventionRecord]:
        """Keep strategies only after their assistant turn and feedback are covered."""

        messages_by_id = {message.id: message for message in all_messages}
        filtered: list[InterventionRecord] = []
        for intervention in interventions:
            if intervention.status not in {
                InterventionStatus.PENDING,
                InterventionStatus.EVALUATED,
            }:
                continue
            assistant_message = messages_by_id.get(intervention.assistant_message_id)
            if assistant_message is None:
                assistant_message = await self._message_repository.get_by_id(
                    user_id,
                    intervention.assistant_message_id
                )
            if (
                assistant_message is None
                or assistant_message.sequence_number > covered_to_sequence
            ):
                continue
            if intervention.status == InterventionStatus.EVALUATED:
                user_response = next(
                    (
                        message
                        for message in all_messages
                        if message.role == MessageRole.USER
                        and message.sequence_number > assistant_message.sequence_number
                    ),
                    None,
                )
                if (
                    user_response is None
                    or user_response.sequence_number > covered_to_sequence
                ):
                    continue
            filtered.append(intervention)
        return filtered


def _filter_state_for_coverage(
    state: SessionState,
    *,
    previous_summary: RollingSummary | None,
    uncovered_messages: list[Message],
) -> SessionState:
    """Remove state facts whose source messages remain outside summary coverage."""

    allowed_source_ids: set[MessageId] = {
        message.id for message in uncovered_messages
    }
    if previous_summary is not None:
        allowed_source_ids.update(previous_summary.source_message_ids)

    def is_covered(message_id: MessageId) -> bool:
        return message_id in allowed_source_ids

    # Scalar state fields have no source IDs in the current public schema. Retain only
    # an already-grounded session goal from the previous summary; do not introduce a
    # new goal, action plan, or risk fact solely from the latest untraceable state.
    return state.model_copy(
        update={
            "session_goal": (
                previous_summary.session_goal if previous_summary is not None else None
            ),
            "active_topics": [
                item for item in state.active_topics if is_covered(item.source.message_id)
            ],
            "reported_emotions": [
                item
                for item in state.reported_emotions
                if is_covered(item.source.message_id)
            ],
            "user_preferences": [
                item
                for item in state.user_preferences
                if is_covered(item.source.message_id)
            ],
            "open_questions": [
                item
                for item in state.open_questions
                if is_covered(item.source.message_id)
            ],
            "pending_action_plan": None,
            "risk_state": RiskState(),
        }
    )


__all__ = ["Summarizer", "SummaryWorker"]
