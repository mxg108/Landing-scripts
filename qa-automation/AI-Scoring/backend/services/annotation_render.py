"""AnnotatedTranscript → readable text — TwoStageScoringDesign §4.

Pure function: renders the Stage-A artifact into the lines the Stage-B
judge (and the smoke script's human reviewers) read. Holds are
interleaved into the turn stream by start time and ALWAYS carry the
"observational" label — the §3.1 wording rule is enforced here, at the
render boundary, so no prompt can accidentally present a heard hold as
a verified system fact.
"""

from __future__ import annotations

from backend.models.formula import AnnotatedTranscript, HoldSegment, TranscriptTurn


def _mmss(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def _render_turn(turn: TranscriptTurn) -> str:
    traits = ", ".join(
        t for t in (turn.emotion, turn.pace_marker) if t
    )
    marks = " [interrupts]" if turn.interruption else ""
    intent = f" ({turn.paraphrase_intent})" if turn.paraphrase_intent else ""
    head = f"[{_mmss(turn.start_ms)} {turn.speaker}"
    if traits:
        head += f" | {traits}"
    return f"{head}]{marks}{intent} \"{turn.text}\""


def _render_hold(hold: HoldSegment) -> str:
    seconds = max(0, hold.end_ms - hold.start_ms) // 1000
    note = f" — {hold.note}" if hold.note else ""
    return (
        f"[{_mmss(hold.start_ms)}→{_mmss(hold.end_ms)} HOLD ~{seconds}s "
        f"({hold.kind}, observational — not system-verified)]{note}"
    )


def render_annotated_transcript(annotation: AnnotatedTranscript) -> str:
    """Turns + holds interleaved by start time, call observations last."""
    events: list[tuple[int, int, str]] = []
    # Sort key: (start_ms, tiebreak) — holds sort ahead of a turn starting
    # at the same instant, mirroring how the listener experiences them.
    for turn in annotation.turns:
        events.append((turn.start_ms or 0, 1, _render_turn(turn)))
    for hold in annotation.holds:
        events.append((hold.start_ms, 0, _render_hold(hold)))
    events.sort(key=lambda e: (e[0], e[1]))

    lines = [
        f"Language detected: {annotation.language_detected or 'unknown'} "
        f"(annotation schema {annotation.schema_version})",
        "",
        *[text for _, _, text in events],
    ]
    if annotation.call_observations:
        lines += ["", "CALL-LEVEL OBSERVATIONS:"]
        lines += [f"- {obs}" for obs in annotation.call_observations]
    return "\n".join(lines)
