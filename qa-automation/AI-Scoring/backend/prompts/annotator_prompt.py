"""Stage-A annotator prompt — TwoStageScoringDesign §3.

The annotator does NOT score. Its one job: turn the audio into the
annotated transcript (`gemini_annotate_v1`) that Stage B judges from —
capturing everything a text-only judge would otherwise lose (tone,
emotion, interruptions, pacing, holds).

Audio-is-SOT survives the split HERE: the annotation is the SOT-carrier
(§3.2), so this prompt owns the v2.1 language rule — for non-English
calls the annotator re-transcribes from audio and treats the Dialpad
transcript as unreliable.
"""

from __future__ import annotations

import json

_SCHEMA_VERSION = "gemini_annotate_v1"

# Gemini response_schema for constrained decoding — hand-authored because
# the API speaks an OpenAPI subset that rejects pydantic's
# additionalProperties:false (400 INVALID_ARGUMENT, observed 2026-07-27).
# Pydantic still validates the parsed result on our side, so the
# extra="forbid" contract holds; a test pins this dict to the model
# fields so the two can't drift.
ANNOTATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "language_detected": {"type": "string"},
        "turns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {
                        "type": "string",
                        "enum": ["agent", "caller", "system", "other"],
                    },
                    "text": {"type": "string"},
                    "emotion": {"type": "string"},
                    "paraphrase_intent": {"type": "string"},
                    "pace_marker": {"type": "string"},
                    "interruption": {"type": "boolean"},
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                },
                "required": ["speaker", "text"],
            },
        },
        "holds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_ms": {"type": "integer"},
                    "end_ms": {"type": "integer"},
                    "kind": {
                        "type": "string",
                        "enum": ["hold_music", "dead_air", "mute_suspected"],
                    },
                    "note": {"type": "string"},
                },
                "required": ["start_ms", "end_ms", "kind"],
            },
        },
        "call_observations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["schema_version", "turns"],
}

_ANNOTATOR_SYSTEM = """You are an audio-as-data interpreter for a QA \
pipeline at Landing, a flexible-living company. You listen to member \
support / sales calls and produce a structured ANNOTATED TRANSCRIPT. \
You never score, judge, or coach — a separate evaluator does that using \
ONLY your annotation. Anything you fail to capture from the audio is \
lost to the evaluator, so be faithful and complete."""

_ANNOTATOR_INSTRUCTIONS = """\
=== YOUR TASK ===
Listen to the attached call audio and produce ONE JSON object with this
exact shape (schema_version "{schema_version}"):

{{
  "schema_version": "{schema_version}",
  "language_detected": "<primary spoken language, ISO 639-1, e.g. "en"/"es">",
  "turns": [
    {{
      "speaker": "agent" | "caller" | "system" | "other",
      "text": "<what was actually said — from the AUDIO>",
      "emotion": "<speaker's tone, e.g. neutral_friendly, frustrated, warm, flat, anxious>",
      "paraphrase_intent": "<one short phrase: what this turn is doing, e.g. 'greeting + identity verification'>",
      "pace_marker": "slow" | "normal" | "rushed",
      "interruption": <true when this turn cuts the other speaker off>,
      "start_ms": <int>, "end_ms": <int>
    }}, ...
  ],
  "holds": [
    {{ "start_ms": <int>, "end_ms": <int>,
       "kind": "hold_music" | "dead_air" | "mute_suspected",
       "note": "<context, e.g. 'agent announced the hold before placing it'>" }}
  ],
  "call_observations": [
    "<call-level observations a per-turn field can't carry: background noise,
     audio quality problems, overall tone arcs, long silences not worth a
     hold entry, notable divergences from the reference transcript>"
  ]
}}

=== RULES ===
- THE AUDIO IS THE SOURCE OF TRUTH. The reference transcript below is a
  hint from an automatic transcriber; where it disagrees with what you
  hear, trust your ears and note material divergences in
  call_observations.
- If the call is NOT in English: the reference transcript is unreliable.
  Re-transcribe from the audio in the spoken language (do NOT translate
  turns to English), and be extra thorough with emotion/pace annotations.
- "turns.text" is verbatim speech. Do not summarize, censor, or clean it
  up beyond removing filler stutters.
- "holds" are what you HEAR (music, dead air, suspected mute) — you are
  an observer, not a system of record. Include an entry for any gap over
  ~10 seconds. Do NOT record gaps shorter than 10 seconds — brief pauses
  are normal conversation, not holds.
- Timestamps are milliseconds from the start of the recording; best
  effort, never omitted.
- Mark "interruption": true only for genuine cut-offs, not backchannel
  ("mm-hm", "right").
- Output the JSON object ONLY — no markdown fences, no commentary.
"""


def build_annotator_system_prompt() -> str:
    return _ANNOTATOR_SYSTEM


def build_annotator_prompt(
    transcript_text: str,
    moments_display: list[dict] | None = None,
) -> str:
    """Render the user-side annotator prompt.

    *transcript_text* is Dialpad's transcript — a HINT, explicitly
    subordinated to the audio. *moments_display* is the full Dialpad
    marker set (C0: filtering is a prompt decision — the annotator gets
    everything and treats markers as hints too)."""
    parts = [_ANNOTATOR_INSTRUCTIONS.format(schema_version=_SCHEMA_VERSION)]

    if moments_display:
        parts.append(
            "=== DIALPAD SIGNAL MARKERS (hints, machine-detected) ===\n"
            + json.dumps(moments_display, indent=2)
        )

    if transcript_text.strip():
        parts.append(
            "=== REFERENCE TRANSCRIPT (hint — the audio overrules it) ===\n"
            + transcript_text.strip()
        )

    return "\n\n".join(parts)
