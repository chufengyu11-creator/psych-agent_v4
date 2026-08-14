"""Rolling summarizer implementations."""

import json
from pathlib import Path
from typing import TypeVar

from llm.structured_client import StructuredLLMClientProtocol
from schemas.common import MessageId
from schemas.intervention import InterventionRecord, InterventionStatus
from schemas.messages import Message, MessageRole
from schemas.summary import RollingSummarizerInput, RollingSummary

ItemT = TypeVar("ItemT")
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rolling_summarizer.md"


class FakeRollingSummarizer:
    """Deterministic summarizer used as a safe fallback and test double."""

    async def run(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Alias for summarize so the class satisfies Agent protocol."""

        return await self.summarize(payload)

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Build a conservative rolling summary from typed inputs."""

        previous = payload.previous_summary
        if not payload.uncovered_messages:
            if previous is not None:
                return previous
            empty_id = MessageId("no_messages")
            return RollingSummary(
                session_id=payload.session_id,
                summary_version=1,
                covered_from=empty_id,
                covered_to=empty_id,
            )

        source_message_ids = _dedupe(
            [
                *(previous.source_message_ids if previous is not None else []),
                *_source_message_ids(payload),
            ]
        )
        important_user_statements = _dedupe(
            [
                *(previous.important_user_statements if previous is not None else []),
                *[message.content for message in _user_messages(payload.uncovered_messages)],
            ]
        )
        strategies_attempted = _dedupe(
            [
                *(previous.strategies_attempted if previous is not None else []),
                *[intervention.strategy for intervention in payload.interventions],
            ]
        )
        strategy_responses = _dedupe(
            [
                *(previous.strategy_responses if previous is not None else []),
                *_strategy_responses(payload.interventions),
            ]
        )
        open_questions = _dedupe(
            [
                *(previous.open_questions if previous is not None else []),
                *[item.value for item in payload.current_state.open_questions],
                *_explicit_user_questions(payload.uncovered_messages),
            ]
        )

        return RollingSummary(
            session_id=payload.session_id,
            summary_version=_summary_version(payload),
            covered_from=_covered_from(payload),
            covered_to=_covered_to(payload),
            current_problem=_current_problem(payload)
            or (previous.current_problem if previous is not None else None),
            session_goal=_grounded_session_goal(payload)
            or (previous.session_goal if previous is not None else None),
            important_user_statements=important_user_statements,
            strategies_attempted=strategies_attempted,
            strategy_responses=strategy_responses,
            open_questions=open_questions,
            source_message_ids=source_message_ids,
        )


class RollingSummarizer:
    """Model-backed rolling summarizer with deterministic fallback behavior."""

    def __init__(
        self,
        llm_client: StructuredLLMClientProtocol | None = None,
        *,
        model_name: str | None = None,
        fallback: FakeRollingSummarizer | None = None,
        prompt_template: str | None = None,
    ) -> None:
        """Create a summarizer with optional structured model dependency."""

        self._llm_client = llm_client
        self._model_name = model_name
        self._fallback = fallback or FakeRollingSummarizer()
        self._prompt_template = prompt_template or _read_prompt(
            _PROMPT_PATH,
            fallback=(
                "You update a rolling conversation summary. Return only a "
                "RollingSummary JSON object grounded in the input."
            ),
        )

    async def run(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Alias for summarize so the class satisfies Agent protocol."""

        return await self.summarize(payload)

    async def summarize(self, payload: RollingSummarizerInput) -> RollingSummary:
        """Summarize older session messages, falling back on client failure."""

        if not payload.uncovered_messages:
            return await self._fallback.summarize(payload)
        if self._llm_client is None:
            return await self._fallback.summarize(payload)
        try:
            summary = RollingSummary.model_validate(
                await self._llm_client.generate_structured(
                    self._build_prompt(payload),
                    RollingSummary,
                    model_name=self._model_name,
                    metadata={"agent": "rolling_summarizer"},
                )
            )
        except Exception:
            return await self._fallback.summarize(payload)
        if not _has_valid_source_ids(summary, payload) or not _has_valid_coverage_relation(
            summary, payload
        ):
            return await self._fallback.summarize(payload)
        merged = _merge_previous_summary(summary, payload.previous_summary)
        return merged.model_copy(
            update={
                "session_id": payload.session_id,
                "summary_version": _summary_version(payload),
                "covered_from": _covered_from(payload),
                "covered_to": _covered_to(payload),
            }
        )

    def _build_prompt(self, payload: RollingSummarizerInput) -> str:
        """Render a structured prompt from the typed summarizer input."""

        payload_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        return (
            f"{self._prompt_template}\n\n"
            "Rules:\n"
            "- Do not diagnose or infer hidden motives.\n"
            "- Cite only source_message_ids present in the input.\n"
            "- Keep pending interventions out of strategy feedback.\n\n"
            f"INPUT_JSON:\n{payload_json}"
        )


def _read_prompt(path: Path, *, fallback: str) -> str:
    """Read an optional prompt file, falling back when absent or empty."""

    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return fallback


def _summary_version(payload: RollingSummarizerInput) -> int:
    """Return the next summary version for the payload."""

    if payload.previous_summary is None:
        return 1
    return payload.previous_summary.summary_version + 1


def _covered_from(payload: RollingSummarizerInput) -> MessageId:
    """Return the first message covered by the summary."""

    if payload.previous_summary is not None:
        return payload.previous_summary.covered_from
    if payload.uncovered_messages:
        return payload.uncovered_messages[0].id
    return MessageId("no_messages")


def _covered_to(payload: RollingSummarizerInput) -> MessageId:
    """Return the last message covered by the summary."""

    if payload.uncovered_messages:
        return payload.uncovered_messages[-1].id
    if payload.previous_summary is not None:
        return payload.previous_summary.covered_to
    return MessageId("no_messages")


def _user_messages(messages: list[Message]) -> list[Message]:
    """Return only user-authored messages."""

    return [message for message in messages if message.role == MessageRole.USER]


def _current_problem(payload: RollingSummarizerInput) -> str | None:
    """Use state facts first, then the first user message as a conservative fallback."""

    parts = [item.value for item in payload.current_state.active_topics if item.active]
    parts.extend(item.value for item in payload.current_state.reported_emotions if item.active)
    if parts:
        return "; ".join(_dedupe(parts))
    user_messages = _user_messages(payload.uncovered_messages)
    if user_messages:
        return user_messages[0].content
    return None


def _grounded_session_goal(payload: RollingSummarizerInput) -> str | None:
    """Use a state goal only when the filtered state still carries source evidence."""

    if payload.current_state.session_goal is None:
        return None
    state_items = [
        *payload.current_state.active_topics,
        *payload.current_state.reported_emotions,
        *payload.current_state.user_preferences,
        *payload.current_state.open_questions,
    ]
    if any(item.active for item in state_items):
        return payload.current_state.session_goal
    return None


def _strategy_responses(interventions: list[InterventionRecord]) -> list[str]:
    """Summarize only evaluated intervention responses."""

    responses: list[str] = []
    for intervention in interventions:
        if intervention.status != InterventionStatus.EVALUATED:
            continue
        details = [
            part
            for part in [
                intervention.observed_response,
                intervention.explicit_feedback,
                intervention.strategy_fit,
                intervention.objective_progress,
            ]
            if part
        ]
        if details:
            responses.append(f"{intervention.strategy}: {'; '.join(details)}")
    return responses


def _explicit_user_questions(messages: list[Message]) -> list[str]:
    """Keep question-like user requests without inventing new open loops."""

    return [
        message.content
        for message in _user_messages(messages)
        if "?" in message.content or "\uff1f" in message.content
    ]


def _source_message_ids(payload: RollingSummarizerInput) -> list[MessageId]:
    """Collect source message IDs for deterministic summary fields."""

    ids = [message.id for message in _user_messages(payload.uncovered_messages)]
    ids.extend(item.source.message_id for item in payload.current_state.active_topics)
    ids.extend(item.source.message_id for item in payload.current_state.reported_emotions)
    ids.extend(item.source.message_id for item in payload.current_state.user_preferences)
    ids.extend(item.source.message_id for item in payload.current_state.open_questions)
    ids.extend(intervention.assistant_message_id for intervention in payload.interventions)
    return _dedupe(ids)


def _allowed_source_message_ids(payload: RollingSummarizerInput) -> list[MessageId]:
    """Collect every source ID a model-backed summary may cite."""

    ids: list[MessageId] = []
    if payload.previous_summary is not None:
        ids.extend(payload.previous_summary.source_message_ids)
    ids.extend(message.id for message in payload.uncovered_messages)
    ids.extend(item.source.message_id for item in payload.current_state.active_topics)
    ids.extend(item.source.message_id for item in payload.current_state.reported_emotions)
    ids.extend(item.source.message_id for item in payload.current_state.user_preferences)
    ids.extend(item.source.message_id for item in payload.current_state.open_questions)
    ids.extend(intervention.assistant_message_id for intervention in payload.interventions)
    return _dedupe(ids)


def _has_valid_source_ids(summary: RollingSummary, payload: RollingSummarizerInput) -> bool:
    """Reject unknown sources and factual summaries without traceable evidence."""

    allowed = set(_allowed_source_message_ids(payload))
    if any(message_id not in allowed for message_id in summary.source_message_ids):
        return False
    if _has_factual_content(summary) and not summary.source_message_ids:
        return False
    return True


def _has_valid_coverage_relation(
    summary: RollingSummary,
    payload: RollingSummarizerInput,
) -> bool:
    """Reject model coverage endpoints outside or reversed within the supplied input."""

    ordered_ids: list[MessageId] = []
    if payload.previous_summary is not None:
        ordered_ids.extend(
            [
                payload.previous_summary.covered_from,
                payload.previous_summary.covered_to,
            ]
        )
    ordered_ids.extend(message.id for message in payload.uncovered_messages)
    ordered_ids = _dedupe(ordered_ids)
    if (
        summary.covered_from not in ordered_ids
        or summary.covered_to not in ordered_ids
    ):
        return False
    return ordered_ids.index(summary.covered_from) <= ordered_ids.index(summary.covered_to)


def _has_factual_content(summary: RollingSummary) -> bool:
    """Return whether the summary contains any user- or strategy-level claim."""

    return any(
        [
            summary.current_problem,
            summary.session_goal,
            *summary.important_user_statements,
            *summary.strategies_attempted,
            *summary.strategy_responses,
            *summary.open_questions,
        ]
    )


def _merge_previous_summary(
    summary: RollingSummary,
    previous: RollingSummary | None,
) -> RollingSummary:
    """Preserve grounded previous facts while appending model-produced updates."""

    if previous is None:
        return summary
    return summary.model_copy(
        update={
            "current_problem": summary.current_problem or previous.current_problem,
            "session_goal": summary.session_goal or previous.session_goal,
            "important_user_statements": _dedupe(
                [
                    *previous.important_user_statements,
                    *summary.important_user_statements,
                ]
            ),
            "strategies_attempted": _dedupe(
                [*previous.strategies_attempted, *summary.strategies_attempted]
            ),
            "strategy_responses": _dedupe(
                [*previous.strategy_responses, *summary.strategy_responses]
            ),
            "open_questions": _dedupe(
                [*previous.open_questions, *summary.open_questions]
            ),
            "source_message_ids": _dedupe(
                [*previous.source_message_ids, *summary.source_message_ids]
            ),
        }
    )


def _dedupe(items: list[ItemT]) -> list[ItemT]:
    """Return items in first-seen order without duplicates."""

    result: list[ItemT] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


__all__ = ["FakeRollingSummarizer", "RollingSummarizer"]
