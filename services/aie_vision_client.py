"""Async client for the lightweight AIE vision service."""

from __future__ import annotations

import httpx


EMPTY_VISION_SUMMARY: dict[str, object] = {
    "window_seconds": 0.0,
    "frames_received": 0,
    "valid_face_frames": 0,
    "dominant_emotion": None,
    "emotion_rank": [],
    "top_active_aus": {},
}


class AIEVisionClient:
    """Best-effort bridge from the main app to the AIE AU/emotion service."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self.available = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start_session(self, session_id: str) -> bool:
        return await self._post_ok("/vision/session/start", {"session_id": session_id})

    async def send_frame(
        self,
        *,
        session_id: str,
        image_base64: str,
        timestamp_ms: int | None,
    ) -> bool:
        return await self._post_ok(
            "/vision/frame",
            {
                "session_id": session_id,
                "format": "jpeg",
                "image_base64": image_base64,
                "timestamp_ms": timestamp_ms,
            },
        )

    async def finish_session(self, session_id: str) -> dict[str, object]:
        if not self.available:
            return dict(EMPTY_VISION_SUMMARY)
        try:
            response = await self._client.post(
                f"{self._base_url}/vision/session/finish",
                json={"session_id": session_id},
            )
            response.raise_for_status()
            payload = response.json()
            summary = payload.get("summary")
            return summary if isinstance(summary, dict) else dict(EMPTY_VISION_SUMMARY)
        except Exception:
            self.available = False
            return dict(EMPTY_VISION_SUMMARY)

    async def _post_ok(self, path: str, payload: dict[str, object]) -> bool:
        if not self.available:
            return False
        try:
            response = await self._client.post(f"{self._base_url}{path}", json=payload)
            response.raise_for_status()
            return True
        except Exception:
            self.available = False
            return False
