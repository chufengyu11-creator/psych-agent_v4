"""Small lazy FunASR wrapper for streamed PCM utterances."""

from __future__ import annotations

import json
import tempfile
import wave
from pathlib import Path
from typing import Any

import anyio


class FunASRUnavailableError(RuntimeError):
    """Raised when FunASR or one of its runtime dependencies is unavailable."""


class FunASRTranscriber:
    """Lazy-load a FunASR model and transcribe PCM16 utterances."""

    def __init__(
        self,
        *,
        model_name: str = "SeacoParaformer",
        vad_model: str = "FsmnVADStreaming",
        punc_model: str = "CTTransformer",
        device: str = "cpu",
        model_dir: str = "/root/.cache/modelscope",
    ) -> None:
        self._model_name = model_name
        self._vad_model = vad_model
        self._punc_model = punc_model
        self._device = device
        self._model_dir = model_dir
        self._model: Any | None = None

    async def transcribe_pcm16(self, pcm16: bytes, *, sample_rate: int) -> str:
        """Transcribe one mono PCM16 utterance."""

        return await anyio.to_thread.run_sync(
            self._transcribe_pcm16_sync,
            pcm16,
            sample_rate,
        )

    def _transcribe_pcm16_sync(self, pcm16: bytes, sample_rate: int) -> str:
        model = self._load_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as audio_file:
            wav_path = Path(audio_file.name)
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm16)
            result = model.generate(input=str(wav_path))
        return _extract_text(result)

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ModuleNotFoundError as error:
            raise FunASRUnavailableError(
                f"FunASR dependency is missing: {error.name}"
            ) from error

        base = getattr(self, "_model_dir", "/root/.cache/modelscope")
        kwargs: dict[str, object] = {
            "model": _resolve_model_path(base, self._model_name),
            "device": self._device,
        }
        if self._vad_model:
            kwargs["vad_model"] = _resolve_model_path(base, self._vad_model)
        if self._punc_model:
            kwargs["punc_model"] = _resolve_model_path(base, self._punc_model)
        self._model = AutoModel(**kwargs)
        return self._model

    def preload(self) -> None:
        """Warm the ASR/VAD/punctuation pipeline so the first utterance is fast."""

        self._load_model()


def _resolve_model_path(base_dir: str, model_name: str) -> str:
    """Map a short model key to its local ModelScope cache directory."""

    import glob
    import os

    candidates: dict[str, str] = {
        "SeacoParaformer": os.path.join(
            base_dir,
            "iic",
            "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        ),
        "FsmnVADStreaming": os.path.join(
            base_dir,
            "iic",
            "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        ),
        "CTTransformer": os.path.join(
            base_dir,
            "iic",
            "punc_ct-transformer_cn-en-common-vocab471067-large",
        ),
    }
    path = candidates.get(model_name)
    if path is not None and os.path.isdir(path):
        return path
    # Fallback: scan the cache directory for a matching model ID fragment
    for entry in glob.glob(os.path.join(base_dir, "iic", "*")):
        if os.path.isdir(entry) and model_name.casefold() in os.path.basename(entry).casefold():
            return entry
    return model_name


def _extract_text(result: object) -> str:
    """Return the concatenated text field from common FunASR outputs."""

    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        text = result.get("text")
        return str(text).strip() if text is not None else json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            if isinstance(item, dict) and item.get("text") is not None:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return str(result).strip()
