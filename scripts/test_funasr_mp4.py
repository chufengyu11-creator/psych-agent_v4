#!/usr/bin/env python3
"""Quick FunASR smoke test for local mp4 files."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def extract_wav(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(target),
    ]
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe an mp4 file with FunASR.")
    parser.add_argument("input", nargs="?", default="raw2.mp4", help="audio/video file")
    parser.add_argument(
        "--model",
        default="paraformer-zh",
        help="FunASR model name or local model path",
    )
    parser.add_argument(
        "--vad-model",
        default="fsmn-vad",
        help="FunASR VAD model name, or empty string to disable",
    )
    parser.add_argument(
        "--punc-model",
        default="ct-punc",
        help="FunASR punctuation model name, or empty string to disable",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="cpu, cuda, cuda:0, etc.",
    )
    args = parser.parse_args()

    source = Path(args.input)
    if not source.exists():
        print(f"input not found: {source}", file=sys.stderr)
        return 2

    wav_path = Path(".tmp") / "asr" / f"{source.stem}.16k.wav"
    print(f"[1/2] extracting wav: {source} -> {wav_path}")
    extract_wav(source, wav_path)

    try:
        from funasr import AutoModel
    except ModuleNotFoundError as error:
        if error.name == "torch":
            print(
                "FunASR is installed, but torch is missing. Install torch first, then rerun.",
                file=sys.stderr,
            )
            return 3
        raise

    print(f"[2/2] loading FunASR model: {args.model}")
    kwargs = {
        "model": args.model,
        "device": args.device,
    }
    if args.vad_model:
        kwargs["vad_model"] = args.vad_model
    if args.punc_model:
        kwargs["punc_model"] = args.punc_model

    model = AutoModel(**kwargs)
    result = model.generate(input=str(wav_path))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
