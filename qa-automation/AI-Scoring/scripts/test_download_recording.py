"""
test_download_recording.py — smoke-test the Dialpad recording download path.

Usage:
    python3 scripts/test_download_recording.py <CALL_ID>
    python3 scripts/test_download_recording.py <CALL_ID> --out /tmp/call.mp3

Verifies the recordings_export scope is wired correctly without going through
the full scoring pipeline. Prints byte count + writes to disk so you can
play the file locally to confirm contents.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Make backend imports resolve when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.dialpad_client import (
    download_recording,
    NoRecordingAvailable,
    DialpadRateLimited,
)


async def main(call_id: str, out_path: Path) -> int:
    try:
        audio_bytes = await download_recording(call_id)
    except NoRecordingAvailable as e:
        print(f"NO RECORDING: {e}")
        return 2
    except DialpadRateLimited as e:
        print(f"RATE LIMITED: {e}")
        return 3
    except RuntimeError as e:
        print(f"CONFIG ERROR: {e}")
        return 4

    out_path.write_bytes(audio_bytes)
    print(f"OK: {len(audio_bytes):,} bytes written to {out_path}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("call_id")
    p.add_argument("--out", default="/tmp/dialpad_recording.mp3", type=Path)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.call_id, args.out)))
