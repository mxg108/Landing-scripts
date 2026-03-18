# Landing QA Automation — Phase 0 Handoff

> **Purpose:** Captures the full state of the project at the end of the Phase 0 prototyping sprint (March 2026).
> Intended as a handoff to Claude Code (VSCode plugin) for continued development.

---

## Overview

This project automates Landing's call center QA scoring process using AI audio and transcript analysis,
freeing managers from manual call review. Phase 0 is a validation phase — prove the concept works
before building any backend infrastructure.

---

## What Was Built in Phase 0

A single-file HTML webapp (`landing-qa-evaluator.html`) that:

1. Accepts a **Dialpad transcript JSON** (pasted from the Dialpad API response)
2. Parses and stitches the transcript into readable dialogue, auto-detecting agent name, caller name, `user_id`, and `contact_id`
3. Surfaces **Dialpad AI signal moments** (`negative_sentiment`, `swearing`, `issue_unresolved`, etc.) as structured metadata passed alongside the transcript
4. Accepts a **SOP document** via PDF upload or Notion URL
5. Calls the **Anthropic Claude API** (`claude-sonnet-4-20250514`) with a structured scoring prompt
6. Returns a **JSON scorecard** with score, confidence level, and grounded reasoning per section
7. Renders results in a visual scorecard UI with per-section confidence badges

The webapp is a self-contained prototype — no backend, no database, no auth. All state lives in-memory.

---

## Dialpad API Integration

### Method Used
`GET /api/v2/call/{call_id}/transcript` — returns the full call transcript as a JSON object.

### Relevant Fields Extracted

| Field | Source | Use |
|---|---|---|
| `call_id` | Root of response | Unique call identifier |
| `user_id` | Transcript lines | Identifies agent turns |
| `contact_id` | Transcript lines | Identifies caller turns |
| `name` | Transcript lines | Human-readable speaker name |
| `content` | Transcript lines | Spoken text |
| `time` | All lines | Timestamp for ordering and duration inference |
| `type: "transcript"` | Lines filter | Actual spoken dialogue |
| `type: "moment"` | Lines filter | Dialpad AI-detected events |
| `type: "real_time_moment"` | Lines filter | Dialpad SOP-triggered events |
| `type: "custom_moment"` | Lines filter | Team-defined keyword triggers |

### Moment Types Leveraged

Dialpad auto-generates these moment types which are passed as bonus signal to the scoring model:
- `negative_sentiment` — member frustration detected
- `swearing` — profanity detected
- `issue_unresolved` — Dialpad flagged the issue as unresolved
- `Addressing Swearing or Insults on Phone Calls` — real-time SOP trigger
- `Member Frustration`, `Lock Outs`, `Early Check-In` — custom team moments

> **Design rationale:** Rather than asking the LLM to infer frustration from transcript text alone,
> Dialpad's own moment metadata is extracted and passed as explicit signal. This reduces hallucination
> risk and makes scoring reasoning auditable.

### Filtered Out (Noise)
The following moment types are stripped before passing to the model:
`whole_call_summary_fragment`, `whole_call_summary`, `ner`, `action_item_v2`, `ai_csat_reboot`,
`call_disposition`, `call_purpose`, `question`

---

## Notion MCP Integration

### Intended Design
The Notion URL tab was designed to fetch SOP content live using the **Notion MCP server**
(`https://mcp.notion.com/mcp`) via the Anthropic API's `mcp_servers` parameter.

### Problem Encountered
The Notion MCP fetch requires a **multi-turn conversation** to complete:
1. First API call → Claude returns `mcp_tool_use` block (calls `notion-fetch` tool)
2. Second API call (with `tool_result`) → Claude returns the final text response

A single API call from a static HTML file cannot manage this loop. The first response returns
`mcp_tool_use` blocks instead of text, and the downstream JSON parser attempted to parse this
as a scorecard, producing:

```
Evaluation failed: Expected property name or '}' in JSON at position 4 (line 2 column 3)
```

### Workaround Implemented
The Member Lockout SOP (`a17d7ec6-8523-42c3-9c8e-35c01fa717ae`) was fetched server-side
and **embedded as a JavaScript constant** in the HTML. When the user pastes the Lockout URL
and clicks "Load SOP", the page ID is extracted and matched against the pre-loaded library
instantly — no API call required.

For unrecognized Notion URLs, a manual text paste fallback is shown.

### Phase 2 Resolution
In the FastAPI backend, the Notion MCP multi-turn loop can be managed correctly server-side.
The pre-loaded constant approach is the right pattern only for static HTML files.

---

## JSON Parse Error — History

### Error 1
```
Evaluation failed: Expected double-quoted property name in JSON at position 3738 (line 85 column 24)
```
**Root cause:** Claude's reasoning text contains apostrophes (`didn't`, `we'll`) which produce
unescaped characters that break `JSON.parse`.

**Fix applied:**
- System prompt explicitly instructs: *"Escape any apostrophes or quotes inside string values. Never use newlines inside string values."*
- Extraction code strips markdown fences, finds the outermost `{}`, removes control characters (`\x00–\x1F`), and has a fallback quote-fixer before throwing

### Error 2
```
Evaluation failed: Expected property name or '}' in JSON at position 4 (line 2 column 3)
```
**Root cause:** Notion fetch API call returned an `mcp_tool_use` block (not text), and the
evaluator tried to parse it as a JSON scorecard.

**Fix applied:** Replaced MCP fetch with pre-loaded SOP constants.

---

## Scoring Architecture

### What Can Be Scored from Transcript Alone

| Section | Automatable? | Confidence Cap | Notes |
|---|---|---|---|
| Greeting | ✅ | High | First-line pattern match |
| Caller Identity Validation | ✅ | High | Binary Y/N detection |
| Purpose of the Call | ✅ | High | Probing question detection |
| Matching the Moment | ⚠️ | Medium max | Tone requires audio; Dialpad moments help |
| Process Adherence | ⚠️ | Medium | Needs SOP context injected |
| Call Resolution | ⚠️ | Medium | Needs SOP context injected |
| Communication | ✅ | High | Language quality, slang, clarity |
| Efficiency & Call Handling | ⚠️ | Medium max | Hold timing requires audio/metadata |
| Documentation | ❌ | N/A | Always manual — never automate |
| Customer Resolution Indicator | ✅ | High | Closing phrase detection |

### Prompt Structure
Each evaluation call sends:
1. `SYSTEM_PROMPT` — strict JSON-only instruction with formatting rules
2. `user` message containing:
   - Stitched transcript (agent/caller labeled)
   - Dialpad signal moments block
   - Full scoring rubric (all 10 sections with level descriptors)
   - SOP content (if loaded)

### Output Schema
```json
{
  "sections": [
    {
      "id": "greeting",
      "name": "Greeting",
      "score": 3,
      "score_type": "numeric",
      "yn_value": null,
      "confidence": "high",
      "reasoning": "Agent answered and identified himself but did not use the full institutional greeting script.",
      "audio_dependent": false,
      "flags": []
    }
  ],
  "overall_notes": "..."
}
```

---

## Current Limitations

1. **Transcript-only** — Audio nuances (tone, language proficiency, hold dead air, confidence)
   cannot be assessed. Matching the Moment and Efficiency are structurally underscored until
   the audio pipeline (Phase 1) is added.
2. **No Dialpad API call** — The webapp requires manual JSON paste. Phase 1 will call
   `GET /v2/call/{id}/transcript` directly via call ID input.
3. **Notion MCP not live in browser** — Pre-loaded SOP constants are the workaround.
   Only the Member Lockout SOP is pre-loaded.
4. **No persistence** — No database, no scoring history, no per-agent tracking.
   Every session is ephemeral.
5. **No manager approval workflow** — AI scores are not written anywhere.
   Phase 1 will write draft scores to Google Sheets.
6. **Section 9 (Documentation) always excluded** — By design. Requires Slack + Mission Control
   cross-platform lookup.
7. **Spanish calls not yet tested** — Gemini 2.5 Flash (Phase 1 target) handles Spanish natively.
   Claude Sonnet handles it but hasn't been validated on Landing's specific call patterns.
8. **`max_tokens: 1000`** — Capped for prototyping cost control. Complex calls may get
   truncated reasoning. Raise to 2000–4000 in Phase 1.

---

## File Inventory

| File | Location | Status |
|---|---|---|
| `landing-qa-evaluator.html` | `/outputs/` | ✅ Working prototype |
| `CLAUDE.md` | Project root | ✅ Canonical context doc |
| `Member_Support_QA_2025__Scorecard_Matrix.pdf` | Project root | ✅ Source of truth rubric |
| `PhaseZero.md` | Project root | ✅ This file |
| `score_call.py` | Not yet created | 🔲 Phase 0 Python deliverable |

---

## Immediate Next Steps (Remaining Phase 0)

1. **Build `score_call.py`** — Standalone Python script that:
   - Accepts a local audio file path as CLI argument
   - Uploads to Gemini 2.5 Flash via `google-generativeai` SDK
   - Uses the same scoring prompt structure from the webapp
   - Outputs JSON scorecard to console + optional `.json` file
   - Dependencies: `google-generativeai`, `python-dotenv`, `argparse`
   - API key stored in `.env` as `GEMINI_API_KEY`

2. **Pull 20–25 real calls from Dialpad** — Diverse sample: textbook great, clearly poor,
   Spanish, and genuinely ambiguous calls

3. **Side-by-side calibration session** — Sit with a manager, compare AI scores to human scores,
   document gaps

4. **Go/no-go decision** before any Phase 1 backend work begins

---

## Tech Stack (Phase 0)

| Layer | Tool |
|---|---|
| AI scoring (webapp) | Claude Sonnet 4 via Anthropic API |
| AI scoring (Python script) | Gemini 2.5 Flash via Google Generative AI SDK |
| Transcript source | Dialpad API `GET /v2/call/{id}/transcript` |
| SOP source | Notion MCP (browser workaround: pre-loaded constants) |
| Frontend | Vanilla HTML/CSS/JS (single file, no framework) |
| Fonts | Fraunces (display) + DM Mono (body) |

---

## Repository

- **GitHub:** `https://github.com/mxg108/Landing-scripts`
- **Target branch:** `feature/ai-scoring`

---

## Key Design Principles (from CLAUDE.md)

- Human review is non-negotiable at launch. AI proposes; managers approve.
- Documentation (Section 9) is always manual. Never build automation for it.
- The existing Apps Script email flow is sacred until Phase 3.
- Build for maintainability over cleverness — this needs to outlast one person's tenure.
- Audio is the source of truth. Dialpad transcripts miss tone, emotion, and audio nuance.
- Cost matters at scale. Gemini Flash at $0.40/M tokens vs GPT-4o at $15/M is a real difference
  at hundreds of calls/week.
