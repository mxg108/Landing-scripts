"""Stage-A annotator smoke — TwoStageScoringDesign P2 (§10.4 trial).

Annotates real Dialpad calls WITHOUT touching the scoring pipeline or
the database, printing the rendered annotation for human review — the
Spanish-manager spot-check reads this output. `--model both` runs
flash AND pro on the same audio for the side-by-side comparison.

Usage (from qa-automation/AI-Scoring, .env loaded):

    .venv/bin/python scripts/annotate_smoke.py --call-id 4501234...
    .venv/bin/python scripts/annotate_smoke.py --call-id ... --model both
    .venv/bin/python scripts/annotate_smoke.py --call-id ... --raw-json

Output goes to stdout and (per model) to
scripts/annotations/<call_id>.<model>.txt for sharing with reviewers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.services.annotation_render import render_annotated_transcript  # noqa: E402
from backend.services.audio_service import annotate_audio  # noqa: E402
from backend.services.dialpad_client import (  # noqa: E402
    download_recording,
    get_transcript,
)

_OUT_DIR = Path(__file__).resolve().parent / "annotations"
# "pro" is Google's rolling alias — gemini-2.5-pro 404s for new API users
# (observed 2026-07-26: "no longer available to new users"). Any raw
# model id also works as --model (e.g. gemini-3-pro-preview,
# gemini-3.5-flash) for one-off trials.
_MODELS = {
    "flash": "gemini-2.5-flash",
    "pro": "gemini-pro-latest",
}


async def _run_one(call_id: str, model: str, audio_bytes: bytes,
                   transcript_data, raw_json: bool):
    started = time.monotonic()
    annotation = await annotate_audio(
        audio_bytes,
        f"{call_id}.mp3",
        transcript_text=transcript_data.get("transcript_text", ""),
        moments_display=transcript_data.get("moments_display", []),
        model=model,
    )
    elapsed = time.monotonic() - started

    rendered = render_annotated_transcript(annotation)
    header = (
        f"===== {call_id} | {model} | {elapsed:.1f}s | "
        f"{len(annotation.turns)} turns, {len(annotation.holds)} holds, "
        f"lang={annotation.language_detected} ====="
    )
    print(f"\n{header}\n{rendered}\n")

    _OUT_DIR.mkdir(exist_ok=True)
    out = _OUT_DIR / f"{call_id}.{model}.txt"
    out.write_text(f"{header}\n\n{rendered}\n", encoding="utf-8")
    if raw_json:
        raw = _OUT_DIR / f"{call_id}.{model}.json"
        raw.write_text(
            json.dumps(annotation.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"(saved → {out})")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call-id", required=True,
                        help="Dialpad call id (the /lookup per-leg id)")
    parser.add_argument("--model", default="flash",
                        help="flash | pro | both | any raw Gemini model id")
    parser.add_argument("--raw-json", action="store_true",
                        help="also save the raw gemini_annotate_v1 JSON")
    args = parser.parse_args()

    print(f"→ downloading recording + transcript for {args.call_id} …")
    audio_bytes = await download_recording(args.call_id)
    transcript_data = await get_transcript(args.call_id)

    keys = ("flash", "pro") if args.model == "both" else (args.model,)
    failures = 0
    for key in keys:
        model = _MODELS.get(key, key)   # alias, or a raw model id
        try:
            await _run_one(args.call_id, model, audio_bytes,
                           transcript_data, args.raw_json)
        except Exception as exc:  # noqa: BLE001 — one model must not kill the comparison
            failures += 1
            print(f"\n!! {model} FAILED: {type(exc).__name__}: {exc}\n")
    return 1 if failures == len(keys) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
