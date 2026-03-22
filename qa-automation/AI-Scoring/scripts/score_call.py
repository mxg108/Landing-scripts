"""
score_call.py — Phase 0 QA Scoring Script
Scores a Dialpad call audio file using Gemini 2.5 Flash.

Usage:
    python3 score_call.py path/to/call.mp3
    python3 score_call.py path/to/call.mp3 --output scorecard.json
    python3 score_call.py path/to/call.mp3 --agent "John Doe" --notes "Complex lockout call"
"""

import argparse
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL = "gemini-2.5-flash"

SUPPORTED_AUDIO_TYPES = {
    ".mp3": "audio/mp3",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a QA evaluator for a call center at Landing Living LLC.
You will listen to a full call recording and score it using the rubric provided.

CRITICAL OUTPUT RULES:
- Return ONLY a valid JSON object. No markdown fences, no prose, no explanations outside the JSON.
- All string values must have apostrophes and internal quotes escaped.
- Never use raw newlines inside string values — use a space instead.
- The JSON must be parseable by Python json.loads() without modification.
"""

SCORING_PROMPT = """Score this call recording using the Landing QA rubric below.

=== SCORING RUBRIC ===

SECTION 1 — Greeting (score: 1–5)
Did the agent use the institutional greeting and answer immediately?
1: Not ready, no script, noisy headset
2: Used script but poor tone, didn't pick up promptly
3: Used script, answered within 5 seconds
4: Missed self-introduction but answered timely
5: Used script, great opening tone, answered immediately

SECTION 2 — Caller Identity Validation (yn_value: "Y", "N", or "NA")
Did the agent verify: full name AND (DOB/email OR last 4 digits of payment method)?
Mark NA if not applicable (e.g., internal call or no sensitive info discussed).

SECTION 3 — Purpose of the Call (score: 1–5)
Did the agent ask relevant questions to understand the issue?
1: Immediate transfer, no probing
2: Repeated the question or asked obvious questions
3: Understood but didn't probe or questions were convoluted
4: Probing questions to get to root cause
5: Multiple probing questions, reinstated problem back to member

SECTION 4 — Matching the Moment (score: 1–5) [AUDIO-DEPENDENT]
Was tone and pace appropriate to the context of the call?
1: Contrary/sarcastic tone, interrupting the guest
2: Disregarded sentiment, monotonous, not assertive
3: Matched the tone
4: 2 of 3: Assurance of assistance, empathy, paraphrasing
5: All 3: Assurance + empathy + paraphrasing reason for call

SECTION 5 — Process Adherence (score: 1–5) [REQUIRES SOP CONTEXT — score conservatively]
Did the agent follow correct process and company policies?
1: Misinformed member, no action, didn't follow process
2: Missing steps, incomplete info, gray areas
3: Right steps but missed something minor
4: Completed right steps
5: Above and beyond, steps followed completely

SECTION 6 — Call Resolution (score: 1–5) [REQUIRES SOP CONTEXT — score conservatively]
Was the issue fully resolved or clear resolution path provided?
1: No solution provided or no follow-up
2: Incorrect solution or improper expectations
3: Found solution but missed small details
4: Handled call, provided resolution but missed next steps
5: Solution found, educated member on process and next steps

SECTION 7 — Communication (score: 1–5)
Clear, concise, helpful information?
1: Confusing, unclear, inappropriate language
2: Landing lingo, grammatical errors, rambling, inappropriate slang
3: Good simple communication
4: Appropriate communication
5: Professional wording, clear/concise, engaging, built rapport

SECTION 8 — Efficiency & Call Handling (score: 1–5)
Handled efficiently without unnecessary delays?
1: Call avoidance, inefficient hold use, didn't announce hold
2: Didn't refresh member timely, wasted time finding solution
3: Assisted but didn't inform caller about time needed
4: Assisted guest in timely manner
5: No hold/dead air OR proper hold expectations set, confident throughout

SECTION 9 — Documentation (SKIP — always manual, never scored by AI)

SECTION 10 — Customer Resolution Indicator (yn_value: "Y", "N", or "NA")
Did agent summarize result/actions AND ask if there is anything else they can do?
Mark NA if not applicable.

=== CONFIDENCE LEVELS ===
- "high": Clear evidence in audio, no ambiguity
- "medium": Reasonable inference, some ambiguity or audio-dependent nuance
- "low": Insufficient signal, heavy guessing required

For Sections 4, 5, 6, 8: cap confidence at "medium" unless there is unambiguous audio evidence.

=== REQUIRED OUTPUT FORMAT ===
Return exactly this JSON structure and nothing else:

{
  "sections": [
    {
      "id": "greeting",
      "name": "Greeting",
      "score": <1-5 integer>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "high",
      "reasoning": "<one or two sentences grounded in what you heard>",
      "audio_dependent": false,
      "flags": []
    },
    {
      "id": "caller_identity_validation",
      "name": "Caller Identity Validation",
      "score": null,
      "score_type": "yn",
      "yn_value": "<Y or N or NA>",
      "confidence": "high",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": []
    },
    {
      "id": "purpose_of_call",
      "name": "Purpose of the Call",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": []
    },
    {
      "id": "matching_the_moment",
      "name": "Matching the Moment",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": true,
      "flags": []
    },
    {
      "id": "process_adherence",
      "name": "Process Adherence",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": ["sop_context_missing"]
    },
    {
      "id": "call_resolution",
      "name": "Call Resolution",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": ["sop_context_missing"]
    },
    {
      "id": "communication",
      "name": "Communication",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": []
    },
    {
      "id": "efficiency_call_handling",
      "name": "Efficiency & Call Handling",
      "score": <1-5>,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": true,
      "flags": []
    },
    {
      "id": "customer_resolution_indicator",
      "name": "Customer Resolution Indicator",
      "score": null,
      "score_type": "yn",
      "yn_value": "<Y or N or NA>",
      "confidence": "<high or medium or low>",
      "reasoning": "<one or two sentences>",
      "audio_dependent": false,
      "flags": []
    }
  ],
  "call_summary": "<a concise summary of the call, in one or two sentences>",
  "key_strengths": "<2-3 specific strengths observed in this call, as a single string>",
  "opportunities": "<2-3 specific coaching opportunities from this call, as a single string>"
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_mime_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in SUPPORTED_AUDIO_TYPES:
        return SUPPORTED_AUDIO_TYPES[ext]
    guessed, _ = mimetypes.guess_type(str(file_path))
    if guessed and guessed.startswith("audio/"):
        return guessed
    raise ValueError(
        f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED_AUDIO_TYPES)}"
    )


def extract_json(text: str) -> dict:
    """Extract and parse JSON from model response, stripping any markdown fences."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())

    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON object found in model response.")

    json_str = text[start:end]
    # Remove non-printable control characters (preserve tab/newline which JSON allows between tokens)
    json_str = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON parse failed: {e}\n\nRaw response snippet:\n{json_str[:600]}")


def print_scorecard(scorecard: dict, audio_file: str) -> None:
    """Pretty-print scorecard to stdout."""
    print(f"\n{'='*62}")
    print(f"  QA SCORECARD — {Path(audio_file).name}")
    meta = scorecard.get("_meta", {})
    if meta.get("agent"):
        print(f"  Agent: {meta['agent']}")
    print(f"{'='*62}\n")

    numeric_scores = []
    for section in scorecard.get("sections", []):
        name = section.get("name", "Unknown")
        score_type = section.get("score_type", "numeric")
        confidence = section.get("confidence", "?").upper()
        reasoning = section.get("reasoning", "")
        flags = section.get("flags", [])
        audio_dep = section.get("audio_dependent", False)

        if score_type == "yn":
            yn = section.get("yn_value", "?")
            score_display = f"[{yn}]"
        else:
            score = section.get("score")
            score_display = f"{score}/5" if score is not None else "N/A"
            if isinstance(score, (int, float)):
                numeric_scores.append(score)

        audio_tag = " [audio]" if audio_dep else ""
        flag_tag = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {name}{audio_tag}")
        print(f"    Score: {score_display}  |  Confidence: {confidence}{flag_tag}")
        print(f"    {reasoning}")
        print()

    print("  Documentation")
    print("    Score: —  |  MANUAL ONLY")
    print("    This section is never AI-scored. Manager review required.")
    print()

    if numeric_scores:
        avg = sum(numeric_scores) / len(numeric_scores)
        print(f"  Scored section average: {avg:.2f}/5")
        print()

    call_summary = scorecard.get("call_summary", "")
    if call_summary:
        print(f"  Call Summary:")
        print(f"    {call_summary}")
        print()

    strengths = scorecard.get("key_strengths", "")
    if strengths:
        print(f"  Key Strengths:")
        print(f"    {strengths}")
        print()

    opps = scorecard.get("opportunities", "")
    if opps:
        print(f"  Opportunities for Improvement:")
        print(f"    {opps}")

    print(f"\n{'='*62}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Score a call recording using the Landing QA rubric via Gemini 2.5 Flash."
    )
    parser.add_argument("audio_file", help="Path to audio file (mp3, mp4, m4a, wav, flac, ogg, webm)")
    parser.add_argument("--output", "-o", help="Save JSON scorecard to this path (e.g. scorecard.json)")
    parser.add_argument("--agent", help="Agent name label for the output")
    parser.add_argument(
        "--notes",
        help='Additional context for the model (e.g. "Spanish call" or "member lockout")',
    )
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set. Add it to your .env file.", file=sys.stderr)
        sys.exit(1)

    audio_path = Path(args.audio_file)
    if not audio_path.exists():
        print(f"ERROR: File not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    mime_type = get_mime_type(audio_path)
    file_size_mb = audio_path.stat().st_size / 1024 / 1024

    print(f"Uploading {audio_path.name} ({file_size_mb:.1f} MB)...")

    client = genai.Client(api_key=api_key)

    # Upload audio to Gemini Files API
    uploaded_file = client.files.upload(
        file=str(audio_path),
        config=types.UploadFileConfig(mime_type=mime_type, display_name=audio_path.name),
    )

    print(f"Upload complete. Scoring with {MODEL}...")

    # Build user message
    prompt_parts = [SCORING_PROMPT]
    if args.agent:
        prompt_parts.append(f"\nAgent name: {args.agent}")
    if args.notes:
        prompt_parts.append(f"Additional context: {args.notes}")
    prompt_parts.append("\n[Audio attached — score the call based on what you hear.]")
    user_prompt = "\n".join(prompt_parts)

    response = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_uri(
                        file_uri=uploaded_file.uri,
                        mime_type=mime_type,
                    ),
                    types.Part.from_text(text=user_prompt),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=65536,
        ),
    )

    raw_text = response.text

    try:
        scorecard = extract_json(raw_text)
    except ValueError as e:
        print(f"\nERROR parsing model response: {e}", file=sys.stderr)
        print("\nRaw model output:\n", raw_text, file=sys.stderr)
        # Clean up uploaded file before exit
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass
        sys.exit(1)

    scorecard["_meta"] = {
        "audio_file": str(audio_path.resolve()),
        "model": MODEL,
        "agent": args.agent or None,
        "notes": args.notes or None,
    }

    print_scorecard(scorecard, args.audio_file)

    if args.output:
        output_path = Path(args.output)
        if output_path.is_dir():
            output_path = output_path / (audio_path.stem + ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(scorecard, indent=2, ensure_ascii=False))
        print(f"Scorecard saved to: {output_path.resolve()}")

    # Clean up uploaded file from Gemini
    try:
        client.files.delete(name=uploaded_file.name)
    except Exception:
        pass


if __name__ == "__main__":
    main()
