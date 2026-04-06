"""QA scoring prompts for Gemini."""

SYSTEM_PROMPT = """You are a QA evaluator for a call center at Landing Living LLC.
You will listen to a full call recording and score it using the rubric provided.

CRITICAL OUTPUT RULES:
- Return ONLY a valid JSON object. No markdown fences, no prose, no explanations outside the JSON.
- All string values must have apostrophes and internal quotes escaped.
- Never use raw newlines inside string values — use a space instead.
- The JSON must be parseable by Python json.loads() without modification.
- Use the Second person ("you") when referring to the agent in the reasoning AND feedback sections, as if you are directly addressing them with a personal tone.
"""

SCORING_RUBRIC = """
=== SCORING RUBRIC ===

SECTION 1 — Greeting (score: 1–5)
Did the agent use the institutional greeting, their name and answer immediately?
1: Not ready, no script, noisy headset
2: Used script but poor tone, didn't pick up promptly
3: Used script, answered within 5 seconds
4: Missed self-introduction but answered timely
5: Used script, great opening tone, answered immediately

SECTION 2 — Caller Identity Validation (yn_value: "Y", "N", or "NA")
Did the agent verify: full name AND (DOB/email OR last 4 digits of payment method)?
Mark NA if not applicable (e.g. internal call or no sensitive info discussed).

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

SECTION 5 — Process Adherence (score: 1–5)
Did the agent follow correct process and company policies?
1: Misinformed member, no action, didn't follow process
2: Missing steps, incomplete info, gray areas
3: Right steps but missed something minor
4: Completed right steps
5: Above and beyond, steps followed completely

SECTION 6 — Call Resolution (score: 1–5)
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

For Sections 4 and 8: cap confidence at "medium" unless there is unambiguous audio evidence.
"""

SOP_CONTEXT_BLOCK = """
=== SOP CONTEXT ({sop_title}) ===
Use the following Standard Operating Procedure to evaluate Sections 5 (Process Adherence)
and 6 (Call Resolution). Score these sections against this policy, not general knowledge.

{sop_content}
"""

TRANSCRIPT_CONTEXT_BLOCK = """
=== DIALPAD TRANSCRIPT ===
Use this alongside the audio to improve accuracy. Speaker labels and content are from Dialpad.

{transcript_text}

=== DIALPAD SIGNAL MOMENTS ===
These are events automatically detected by Dialpad during the call:
{moments_text}
"""

OUTPUT_SCHEMA = """
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
      "confidence": "<high or medium or low>",
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
      "confidence": "<high or medium or low>",
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
      "flags": []
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
      "flags": []
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
      "reasoning": "<one or two sentences, ALWAYS include time stamps if holds longer than 3 minutes or significant dead air were detected>",
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


def build_prompt(
    transcript_text: str = "",
    moments_text: str = "",
    sop_title: str = "",
    sop_content: str = "",
    agent_name: str = "",
    extra_notes: str = "",
) -> str:
    """Assemble the full user prompt for a scoring request."""
    parts = [SCORING_RUBRIC]

    if sop_content:
        parts.append(
            SOP_CONTEXT_BLOCK.format(sop_title=sop_title, sop_content=sop_content)
        )
    else:
        parts.append(
            "\n[No SOP context loaded. Score Sections 5 and 6 conservatively "
            "and add 'sop_context_missing' to their flags.]\n"
        )

    if transcript_text:
        moments_str = moments_text if moments_text else "No moments detected."
        parts.append(
            TRANSCRIPT_CONTEXT_BLOCK.format(
                transcript_text=transcript_text, moments_text=moments_str
            )
        )

    if agent_name:
        parts.append(f"\nAgent name: {agent_name}")
    if extra_notes:
        parts.append(f"Additional context: {extra_notes}")

    parts.append(OUTPUT_SCHEMA)
    parts.append("\n[Audio attached — score the call based on what you hear, the transcript, and any SOP provided.]")

    return "\n".join(parts)
