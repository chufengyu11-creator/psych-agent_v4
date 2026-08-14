"""Call the three structured agents against the configured real LLM endpoint."""

# ruff: noqa: E402 -- direct script execution needs the repository root on sys.path.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.risk_agent import RiskAgent
from agents.state_tracker import StateTracker
from agents.strategy_planner import StrategyPlanner
from app.config import Settings
from llm.client import ManagedLLMClient
from llm.exceptions import LLMConfigurationError
from llm.local_client import OpenAICompatibleHTTPClient
from llm.retry_policy import RetryPolicy
from llm.structured_client import StructuredLLMClient
from llm.structured_output import ModelT
from schemas.common import MessageId, SessionId
from schemas.messages import Message, MessageRole
from schemas.risk import RiskInput
from schemas.state import SessionState, StateDelta, StateTrackerInput
from schemas.strategy import StrategyPlannerInput
from scripts.check_llm_endpoint import EndpointExitCode, provider_kind, safe_display_base_url

if TYPE_CHECKING:
    from llm.structured_client import StructuredLLMClientProtocol


@dataclass(frozen=True)
class SmokeOptions:
    """Command options separated from argparse for unit tests."""

    json_output: bool = False
    timeout_seconds: float | None = None


@dataclass
class StructuredCallObservation:
    """Safe observation of one real structured model call."""

    agent: str
    model: str
    latency_ms: int
    api_success: bool
    schema_success: bool
    error_type: str | None = None


@dataclass
class AgentSmokeResult:
    """One Agent outcome that distinguishes model success from fallback."""

    agent: str
    model: str
    latency_ms: int
    api_success: bool
    schema_success: bool
    fallback_used: bool
    effective_result_available: bool
    key_fields: dict[str, object] = field(default_factory=dict)
    source_ids_valid: bool | None = None
    error_type: str | None = None


@dataclass
class StructuredAgentSmokeSummary:
    """Machine-readable summary without prompts, secrets, or hidden reasoning."""

    provider_kind: str
    base_url: str
    model: str
    agents: list[AgentSmokeResult] = field(default_factory=list)
    api_call_count: int = 0
    success: bool = False
    error_type: str | None = None


class ObservingStructuredClient:
    """Record safe call outcomes while preserving the formal structured adapter."""

    def __init__(
        self,
        delegate: StructuredLLMClientProtocol,
        http_client: ManagedLLMClient,
    ) -> None:
        self._delegate = delegate
        self._http_client = http_client
        self.observations: list[StructuredCallObservation] = []

    async def generate_structured(
        self,
        prompt: str,
        output_model: type[ModelT],
        *,
        model_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Delegate unchanged while recording HTTP and schema success separately."""

        agent_name = str((metadata or {}).get("agent", output_model.__name__))
        selected_model = model_name or self._http_client.model_name
        successful_before = self._http_client.successful_http_response_count
        started = monotonic()
        try:
            result = await self._delegate.generate_structured(
                prompt,
                output_model,
                model_name=model_name,
                metadata=metadata,
            )
        except Exception as exc:
            self.observations.append(
                StructuredCallObservation(
                    agent=agent_name,
                    model=selected_model,
                    latency_ms=_elapsed_ms(started),
                    api_success=(
                        self._http_client.successful_http_response_count > successful_before
                    ),
                    schema_success=False,
                    error_type=type(exc).__name__,
                )
            )
            raise
        self.observations.append(
            StructuredCallObservation(
                agent=agent_name,
                model=selected_model,
                latency_ms=_elapsed_ms(started),
                api_success=(
                    self._http_client.successful_http_response_count > successful_before
                ),
                schema_success=True,
            )
        )
        return result


def build_parser() -> argparse.ArgumentParser:
    """Build the structured-Agent smoke CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", type=float, dest="timeout_seconds")
    return parser


def parse_options(argv: Sequence[str] | None = None) -> SmokeOptions:
    """Parse command-line options into a testable value object."""

    args = build_parser().parse_args(argv)
    return SmokeOptions(
        json_output=args.json_output,
        timeout_seconds=args.timeout_seconds,
    )


async def run_structured_agent_smoke(
    settings: Settings,
    options: SmokeOptions,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_policy: RetryPolicy | None = None,
) -> tuple[StructuredAgentSmokeSummary, EndpointExitCode]:
    """Call RiskAgent, StateTracker, and StrategyPlanner exactly once each."""

    summary = StructuredAgentSmokeSummary(
        provider_kind=provider_kind(settings.llm_base_url),
        base_url=safe_display_base_url(settings.llm_base_url),
        model=settings.structured_model_name.strip() or "<missing>",
    )
    try:
        if options.timeout_seconds is not None and options.timeout_seconds <= 0:
            msg = "timeout override must be greater than zero"
            raise LLMConfigurationError(msg)
        http_client = OpenAICompatibleHTTPClient.from_settings(
            settings,
            timeout_seconds=options.timeout_seconds,
            retry_policy=retry_policy,
            transport=transport,
        )
    except LLMConfigurationError as exc:
        summary.error_type = type(exc).__name__
        return summary, EndpointExitCode.CONFIGURATION

    summary.base_url = http_client.base_url
    formal_structured_client = StructuredLLMClient(
        http_client,
        default_model_name=settings.structured_model_name,
    )
    observed_client = ObservingStructuredClient(formal_structured_client, http_client)

    session_id = SessionId("session_real_structured_smoke")
    risk_message = _message(
        "msg_real_risk_1",
        session_id,
        "I have a routine planning question and feel calm today.",
    )
    risk_result = await RiskAgent(
        observed_client,
        model_name=settings.safety_model_name,
    ).analyze(RiskInput(current_message=risk_message))
    summary.agents.append(
        _agent_result(
            observed_client.observations[-1],
            risk_result,
            key_fields={
                "risk_level": risk_result.risk_level.value,
                "route": risk_result.route.value,
            },
        )
    )

    state_message = _message(
        "msg_real_state_1",
        session_id,
        "My small goal is to plan one short break after lunch today.",
    )
    state_result = await StateTracker(
        observed_client,
        model_name=settings.structured_model_name,
    ).extract_delta(
        StateTrackerInput(
            current_message=state_message,
            previous_state=SessionState(session_id=session_id),
        )
    )
    source_ids_valid = _source_ids_are_valid(
        state_result,
        allowed_ids={state_message.id},
    )
    summary.agents.append(
        _agent_result(
            observed_client.observations[-1],
            state_result,
            key_fields={
                "topic_update_count": len(state_result.topic_updates),
                "goal_update_count": len(state_result.goal_updates),
            },
            source_ids_valid=source_ids_valid,
        )
    )

    strategy_result = await StrategyPlanner(
        observed_client,
        model_name=settings.structured_model_name,
    ).plan(
        StrategyPlannerInput(
            session_state=SessionState(
                session_id=session_id,
                session_goal="Plan one manageable break after lunch.",
            ),
            risk=risk_result,
        )
    )
    summary.agents.append(
        _agent_result(
            observed_client.observations[-1],
            strategy_result,
            key_fields={
                "conversation_phase": strategy_result.conversation_phase.value,
                "primary_strategy": strategy_result.primary_strategy.value,
            },
        )
    )

    summary.api_call_count = http_client.request_count
    summary.success = all(
        result.api_success
        and result.schema_success
        and result.effective_result_available
        and result.source_ids_valid is not False
        for result in summary.agents
    )
    exit_code = EndpointExitCode.SUCCESS if summary.success else EndpointExitCode.PROTOCOL
    await http_client.aclose()
    return summary, exit_code


def render_summary(summary: StructuredAgentSmokeSummary, *, json_output: bool) -> str:
    """Render safe Agent diagnostics without model inputs or full outputs."""

    payload = asdict(summary)
    if json_output:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)
    lines = [
        f"provider_kind={summary.provider_kind}",
        f"base_url={summary.base_url}",
        f"model={summary.model}",
    ]
    for result in summary.agents:
        lines.append(
            " ".join(
                [
                    f"agent={result.agent}",
                    f"model={result.model}",
                    f"latency_ms={result.latency_ms}",
                    f"api_success={result.api_success}",
                    f"schema_success={result.schema_success}",
                    f"fallback_used={result.fallback_used}",
                    f"effective_result_available={result.effective_result_available}",
                    f"source_ids_valid={result.source_ids_valid}",
                    f"key_fields={result.key_fields}",
                    f"error_type={result.error_type}",
                ]
            )
        )
    lines.extend(
        [
            f"api_call_count={summary.api_call_count}",
            f"success={summary.success}",
            f"error_type={summary.error_type}",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the real structured-Agent smoke from shared Settings."""

    options = parse_options(argv)
    try:
        settings = Settings()
    except ValidationError:
        summary = StructuredAgentSmokeSummary(
            provider_kind="unknown",
            base_url="<invalid-settings>",
            model="<invalid-settings>",
            error_type="SettingsValidationError",
        )
        print(render_summary(summary, json_output=options.json_output))
        return int(EndpointExitCode.CONFIGURATION)
    summary, exit_code = asyncio.run(run_structured_agent_smoke(settings, options))
    print(render_summary(summary, json_output=options.json_output))
    return int(exit_code)


def _message(message_id: str, session_id: SessionId, content: str) -> Message:
    return Message(
        id=MessageId(message_id),
        session_id=session_id,
        role=MessageRole.USER,
        content=content,
        sequence_number=1,
    )


def _agent_result(
    observation: StructuredCallObservation,
    result: BaseModel,
    *,
    key_fields: dict[str, object],
    source_ids_valid: bool | None = None,
) -> AgentSmokeResult:
    return AgentSmokeResult(
        agent=observation.agent,
        model=observation.model,
        latency_ms=observation.latency_ms,
        api_success=observation.api_success,
        schema_success=observation.schema_success,
        fallback_used=not observation.schema_success,
        effective_result_available=isinstance(result, BaseModel),
        key_fields=key_fields,
        source_ids_valid=source_ids_valid,
        error_type=observation.error_type,
    )


def _source_ids_are_valid(delta: StateDelta, *, allowed_ids: set[MessageId]) -> bool:
    source_ids = {
        item.source_message_id
        for collection in (
            delta.topic_updates,
            delta.goal_updates,
            delta.reported_emotions,
            delta.user_corrections,
            delta.strategy_preferences,
            delta.hypotheses,
        )
        for item in collection
    }
    return source_ids.issubset(allowed_ids)


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1000))


if __name__ == "__main__":
    raise SystemExit(main())
