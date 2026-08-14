"""WebSocket endpoint for lightweight speech + visual context turns."""

from __future__ import annotations

import audioop
import base64
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings
from runtime.application import ApplicationRuntime
from schemas.common import SessionId, UserId
from services.aie_vision_client import EMPTY_VISION_SUMMARY, AIEVisionClient
from services.funasr_transcriber import FunASRTranscriber, FunASRUnavailableError
from services.timing import bind_new_trace, reset_trace, timing_span

router = APIRouter()
_TRANSCRIBER = FunASRTranscriber()
_TRANSCRIBER.preload()


@dataclass
class UtteranceState:
    """Mutable state for one WebSocket recording session."""

    user_id: str | None = None
    session_id: str | None = None
    sample_rate: int = 16_000
    in_speech: bool = False
    processing: bool = False
    pcm_buffer: bytearray = field(default_factory=bytearray)
    speech_ms: float = 0.0
    silence_ms: float = 0.0
    utterance_ms: float = 0.0
    utterance_start_ms: int | None = None
    vision_session_id: str | None = None
    vision_available: bool = False

    def reset_utterance(self) -> None:
        self.in_speech = False
        self.processing = False
        self.pcm_buffer.clear()
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.utterance_ms = 0.0
        self.utterance_start_ms = None
        self.vision_session_id = None
        self.vision_available = False


@router.websocket("/ws")
async def multimodal_ws(websocket: WebSocket) -> None:
    """Receive PCM audio/video frames and return agent responses per utterance."""

    await websocket.accept()
    settings = get_settings()
    runtime = getattr(websocket.app.state, "runtime", None)
    if not isinstance(runtime, ApplicationRuntime):
        await _safe_send_json(
            websocket,
            {"type": "error", "message": "Application runtime is not initialized"},
        )
        await websocket.close(code=1011)
        return

    state = UtteranceState()
    vision_client = AIEVisionClient(settings.aie_vision_url)
    await _safe_send_json(websocket, {"type": "listening"})
    try:
        while True:
            payload = await websocket.receive_json()
            message_type = payload.get("type")
            if message_type == "start":
                state.user_id = _string_or_none(payload.get("user_id"))
                state.session_id = _string_or_none(payload.get("session_id"))
                state.reset_utterance()
                vision_client.available = True
                await _safe_send_json(websocket, {"type": "listening"})
            elif message_type == "audio":
                await _handle_audio_message(
                    websocket,
                    runtime,
                    vision_client,
                    state,
                    payload,
                    silence_limit_ms=settings.multimodal_silence_ms,
                    min_speech_ms=settings.multimodal_min_speech_ms,
                    max_utterance_ms=settings.multimodal_max_utterance_ms,
                    rms_threshold=settings.multimodal_rms_threshold,
                )
            elif message_type == "video_frame":
                await _handle_video_frame(vision_client, state, payload)
            elif message_type == "stop":
                if state.in_speech and state.pcm_buffer:
                    await _finalize_utterance(websocket, runtime, vision_client, state)
                await _safe_send_json(websocket, {"type": "stopped"})
            else:
                await _safe_send_json(
                    websocket,
                    {"type": "error", "message": "Unknown multimodal message type"},
                )
    except WebSocketDisconnect:
        return
    except RuntimeError as exc:
        if "not connected" in str(exc):
            return
        raise
    finally:
        await vision_client.aclose()


async def _handle_audio_message(
    websocket: WebSocket,
    runtime: ApplicationRuntime,
    vision_client: AIEVisionClient,
    state: UtteranceState,
    payload: dict[str, object],
    *,
    silence_limit_ms: int,
    min_speech_ms: int,
    max_utterance_ms: int,
    rms_threshold: int,
) -> None:
    if state.processing or not state.user_id or not state.session_id:
        return
    pcm64 = _string_or_none(payload.get("pcm16_base64"))
    if not pcm64:
        return
    try:
        pcm = base64.b64decode(pcm64)
    except Exception:
        await _safe_send_json(websocket, {"type": "error", "message": "Invalid audio base64"})
        return
    sample_rate = int(payload.get("sample_rate") or 16_000)
    timestamp_ms = _int_or_none(payload.get("timestamp_ms"))
    chunk_ms = _pcm_duration_ms(pcm, sample_rate)
    rms = audioop.rms(pcm, 2) if pcm else 0
    voiced = rms >= rms_threshold

    if voiced and not state.in_speech:
        state.in_speech = True
        state.sample_rate = sample_rate
        state.utterance_start_ms = timestamp_ms
        state.vision_session_id = f"vision_{uuid4().hex}"
        state.vision_available = await vision_client.start_session(state.vision_session_id)
        await _safe_send_json(websocket, {"type": "speech_start"})

    if not state.in_speech:
        return

    state.pcm_buffer.extend(pcm)
    state.utterance_ms += chunk_ms
    if voiced:
        state.speech_ms += chunk_ms
        state.silence_ms = 0.0
    else:
        state.silence_ms += chunk_ms

    should_finalize = (
        state.speech_ms >= min_speech_ms
        and state.silence_ms >= silence_limit_ms
    ) or state.utterance_ms >= max_utterance_ms
    if should_finalize:
        await _finalize_utterance(websocket, runtime, vision_client, state)


async def _handle_video_frame(
    vision_client: AIEVisionClient,
    state: UtteranceState,
    payload: dict[str, object],
) -> None:
    if not state.in_speech or not state.vision_session_id or not state.vision_available:
        return
    image_base64 = _string_or_none(payload.get("image_base64"))
    if not image_base64:
        return
    await vision_client.send_frame(
        session_id=state.vision_session_id,
        image_base64=image_base64,
        timestamp_ms=_int_or_none(payload.get("timestamp_ms")),
    )


async def _finalize_utterance(
    websocket: WebSocket,
    runtime: ApplicationRuntime,
    vision_client: AIEVisionClient,
    state: UtteranceState,
) -> None:
    if state.processing:
        return
    state.processing = True
    trace_token = bind_new_trace()
    try:
        pcm = bytes(state.pcm_buffer)
        with timing_span("multimodal.asr"):
            transcript = await _TRANSCRIBER.transcribe_pcm16(
                pcm,
                sample_rate=state.sample_rate,
            )
        if not transcript:
            await _safe_send_json(
                websocket,
                {"type": "error", "message": "ASR returned empty text"},
            )
            state.reset_utterance()
            return
        if not await _safe_send_json(
            websocket,
            {"type": "utterance_final", "transcript": transcript},
        ):
            return

        summary = dict(EMPTY_VISION_SUMMARY)
        if state.vision_session_id:
            with timing_span("multimodal.vision_summary"):
                summary = await vision_client.finish_session(state.vision_session_id)
        if not await _safe_send_json(
            websocket,
            {"type": "vision_summary", "summary": summary},
        ):
            return

        nonverbal_observations = (
            {"source": "aie_server", "summary": summary}
            if int(summary.get("frames_received") or 0) > 0
            else None
        )
        with timing_span("multimodal.agent_turn"):
            result = await runtime.orchestrator.handle_turn(
                user_id=UserId(state.user_id or ""),
                session_id=SessionId(state.session_id or ""),
                text=transcript,
                nonverbal_observations=nonverbal_observations,
            )
        await _safe_send_json(
            websocket,
            {
                "type": "assistant_response",
                "response": result.response,
                "message_id": str(result.message_id),
                "state_version": result.state_version,
                "session_id": str(result.session_id),
            }
        )
    except FunASRUnavailableError as error:
        await _safe_send_json(websocket, {"type": "error", "message": str(error)})
    except Exception as error:
        await _safe_send_json(websocket, {"type": "error", "message": str(error)})
    finally:
        reset_trace(trace_token)
        state.reset_utterance()


async def _safe_send_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send a WebSocket payload unless the browser has already disconnected."""

    try:
        await websocket.send_json(payload)
    except (RuntimeError, WebSocketDisconnect):
        return False
    return True


def _pcm_duration_ms(pcm: bytes, sample_rate: int) -> float:
    if sample_rate <= 0:
        return 0.0
    return len(pcm) / 2 / sample_rate * 1000.0


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
