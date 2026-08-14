#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP frame ingestion service for AU/emotion summaries."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.apiEmotion import apiEmo


class StartRequest(BaseModel):
    session_id: str


class FrameRequest(BaseModel):
    session_id: str
    format: str = "jpeg"
    image_base64: str
    timestamp_ms: int | None = None


class FinishRequest(BaseModel):
    session_id: str


@dataclass
class VisionSession:
    session_id: str
    started_at: float = field(default_factory=time.monotonic)
    frames_received: int = 0
    valid_face_frames: int = 0
    seen_sequences: set[int] = field(default_factory=set)
    emotion_counts: dict[str, int] = field(default_factory=dict)
    emotion_confidence_sums: dict[str, float] = field(default_factory=dict)
    au_sums: dict[str, float] = field(default_factory=dict)
    au_counts: dict[str, int] = field(default_factory=dict)

    def add_result(self, result: dict[str, Any]) -> None:
        sequence = int(result.get("image_sequence") or 0)
        if sequence <= 0 or sequence in self.seen_sequences:
            return
        self.seen_sequences.add(sequence)
        self.valid_face_frames += 1

        label = str(result.get("emotion_label") or "").strip()
        confidence = float(result.get("emotion_confidence") or 0.0)
        if label:
            self.emotion_counts[label] = self.emotion_counts.get(label, 0) + 1
            self.emotion_confidence_sums[label] = (
                self.emotion_confidence_sums.get(label, 0.0) + confidence
            )

        au_units = result.get("au_units")
        if isinstance(au_units, dict):
            for raw_name, raw_value in au_units.items():
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                name = str(raw_name)
                self.au_sums[name] = self.au_sums.get(name, 0.0) + value
                self.au_counts[name] = self.au_counts.get(name, 0) + 1

    def summary(self) -> dict[str, object]:
        rank = []
        total = sum(self.emotion_counts.values())
        for label, count in self.emotion_counts.items():
            frequency = count / total if total else 0.0
            avg_confidence = self.emotion_confidence_sums[label] / count if count else 0.0
            rank.append(
                {
                    "label": label,
                    "frequency": round(frequency, 4),
                    "avg_confidence": round(avg_confidence, 4),
                    "score": round(frequency * avg_confidence, 4),
                }
            )
        rank.sort(key=lambda item: item["score"], reverse=True)
        top_aus = {
            name: round(self.au_sums[name] / count, 4)
            for name, count in self.au_counts.items()
            if count > 0 and self.au_sums[name] / count > 0.1
        }
        top_aus = dict(
            sorted(top_aus.items(), key=lambda item: item[1], reverse=True)[:8]
        )
        return {
            "window_seconds": round(time.monotonic() - self.started_at, 3),
            "frames_received": self.frames_received,
            "valid_face_frames": self.valid_face_frames,
            "dominant_emotion": rank[0]["label"] if rank else None,
            "emotion_rank": rank,
            "top_active_aus": top_aus,
        }


app = FastAPI(title="AIE AU/Emotion Vision Service")
_api: apiEmo | None = None
_ready = False
_sessions: dict[str, VisionSession] = {}
_sequence_to_session: dict[int, str] = {}
_lock = asyncio.Lock()
_poll_task: asyncio.Task[None] | None = None


@app.on_event("startup")
async def startup() -> None:
    global _api, _ready, _poll_task
    _api = apiEmo(
        device=os.environ.get("AIE_DEVICE", "cuda"),
        mqtt_enable=False,
        gaze_only=False,
        au_only=True,
        heart_rate_only=False,
    )
    _ready = _api.init() and _api.start_processing_threads({"face_detection", "au"})
    _poll_task = asyncio.create_task(_poll_results())


@app.on_event("shutdown")
async def shutdown() -> None:
    global _poll_task
    if _poll_task is not None:
        _poll_task.cancel()
    if _api is not None:
        _api.cleanup()


@app.post("/vision/session/start")
async def start_session(request: StartRequest) -> dict[str, object]:
    _require_ready()
    async with _lock:
        _sessions[request.session_id] = VisionSession(session_id=request.session_id)
    return {"status": "ok", "session_id": request.session_id}


@app.post("/vision/frame")
async def ingest_frame(request: FrameRequest) -> dict[str, object]:
    _require_ready()
    frame = _decode_jpeg(request.image_base64)
    async with _lock:
        session = _sessions.get(request.session_id)
        if session is None:
            session = VisionSession(session_id=request.session_id)
            _sessions[request.session_id] = session
        session.frames_received += 1

    assert _api is not None
    assert _api.data_manager is not None
    timestamp = request.timestamp_ms if request.timestamp_ms is not None else time.time()
    _api.data_manager.broadcast_image_to_all_threads(frame, timestamp)
    sequence = _api.data_manager.get_current_sequence()
    async with _lock:
        _sequence_to_session[int(sequence)] = request.session_id
    return {"status": "accepted", "image_sequence": int(sequence)}


@app.post("/vision/session/finish")
async def finish_session(request: FinishRequest) -> dict[str, object]:
    _require_ready()
    async with _lock:
        session = _sessions.pop(request.session_id, None)
        for sequence, session_id in list(_sequence_to_session.items()):
            if session_id == request.session_id:
                _sequence_to_session.pop(sequence, None)
    if session is None:
        return {"status": "missing", "summary": _empty_summary()}
    return {"status": "ok", "summary": session.summary()}


async def _poll_results() -> None:
    last_sequence = 0
    while True:
        await asyncio.sleep(0.03)
        if not _ready or _api is None:
            continue
        result = _api.getEmoAU()
        if not result:
            continue
        sequence = int(result.get("image_sequence") or 0)
        if sequence <= last_sequence:
            continue
        last_sequence = sequence
        async with _lock:
            session_id = _sequence_to_session.get(sequence)
            session = _sessions.get(session_id or "")
            if session is not None:
                session.add_result(result)


def _decode_jpeg(image_base64: str) -> np.ndarray:
    try:
        raw = base64.b64decode(image_base64)
    except Exception as error:
        raise HTTPException(status_code=400, detail="Invalid image base64") from error
    array = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Invalid JPEG frame")
    return frame


def _require_ready() -> None:
    if not _ready:
        raise HTTPException(status_code=503, detail="AIE AU/emotion pipeline is not ready")


def _empty_summary() -> dict[str, object]:
    return {
        "window_seconds": 0.0,
        "frames_received": 0,
        "valid_face_frames": 0,
        "dominant_emotion": None,
        "emotion_rank": [],
        "top_active_aus": {},
    }
