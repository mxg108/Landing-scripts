# Landing QA Automation — Project Briefing for Claude Code

> This file is the canonical context document for this project.
> Read it fully before writing any code or making any suggestions.

---

## Who I Am & What This Is

I'm a Customer Service Manager at Landing Living LLC. who does light software engineering on the side and I'm looking to build a robust portfolio and learn best SWE practices, methods, and workflows to automate real-world issues we face at Landing.
This project automates Landing's QA process for call center agents using AI audio analysis.
I'm building this to outlast my tenure — it needs to be robust, documented, and maintainable by others.

My existing GitHub already contains a Google Apps Script that sends email notifications when
managers complete a QA Google Form (results auto-populate a Google Sheet). That flow stays
untouched until Phase 3.

---

## The Problem

Current QA approach: managers manually score calls using a rubric. Some teams upload Dialpad
transcripts to a ChatGPT or Grok project trained with SOPs/policies. This misses critical
audio nuances — tone, language proficiency, instances of disrespect — that only listening
to the actual call can catch. Landing also handles calls in Spanish although those calls are outliers, we still need to be able to account for them and correctly evaluate them. Heavy accents are also commonplace.

We have: many Analysts, few Managers, no dedicated QA team. Managers are the bottleneck.

---

## The QA Scorecard (Source of Truth)

Each section is scored 1–5 (Poor → Excellent). Understand these deeply — all AI prompts
will be structured around them.

### 1. Greeting (1–5)
Did the agent use the institutional greeting and answer immediately?
- 1: Not ready, no script, noisy headset
- 2: Used script but poor tone, didn't pick up promptly
- 3: Used script, answered within 5 seconds
- 4: Missed self-introduction but answered timely
- 5: Used script, great opening tone, answered immediately

### 2. Caller Identity Validation (Y/N or N/A)
Did the agent verify: full name AND (DOB/email OR last 4 digits of payment method)?
Binary check. Mark N/A if not applicable.

### 3. Purpose of the Call (1–5)
Did the agent ask relevant questions to understand the issue?
- 1: Immediate transfer, no probing
- 2: Repeated the question or asked obvious questions
- 3: Understood but didn't probe or questions were convoluted
- 4: Probing questions to get to root cause
- 5: Multiple probing questions, reinstated problem back to member

### 4. Matching the Moment (1–5) ⚠️ AUDIO-DEPENDENT
Was tone and pace appropriate to the context of the call?
- 1: Contrary/sarcastic tone, interrupting the guest
- 2: Disregarded sentiment, monotonous, not assertive
- 3: Matched the tone
- 4: 2 of 3: Assurance of assistance, empathy, paraphrasing
- 5: All 3: Assurance + empathy + paraphrasing reason for call

### 5. Process Adherence (1–5) ⚠️ REQUIRES SOP CONTEXT
Did the agent follow correct process and company policies?
- 1: Misinformed member, no action, didn't follow process
- 2: Missing steps, incomplete info, gray areas
- 3: Right steps but missed something minor
- 4: Completed right steps
- 5: Above and beyond, steps followed completely

### 6. Call Resolution (1–5) ⚠️ REQUIRES SOP CONTEXT
Was the issue fully resolved or clear resolution path provided?
- 1: No solution provided or no follow-up
- 2: Incorrect solution or improper expectations
- 3: Found solution but missed small details
- 4: Handled call, provided resolution but missed next steps
- 5: Solution found, educated member on process and next steps

### 7. Communication (1–5)
Clear, concise, helpful information?
- 1: Confusing, unclear, inappropriate language
- 2: Landing lingo, grammatical errors, rambling, inappropriate slang
- 3: Good simple communication
- 4: Appropriate communication
- 5: Professional wording, clear/concise, engaging, built rapport

### 8. Efficiency & Call Handling (1–5)
Handled efficiently without unnecessary delays?
- 1: Call avoidance, inefficient hold use, didn't announce hold
- 2: Didn't refresh member timely, wasted time finding solution
- 3: Assisted but didn't inform caller about time needed
- 4: Assisted guest in timely manner
- 5: No hold/dead air OR proper hold expectations set, confident throughout

### 9. Documentation (1–5) ❌ NOT AUTOMATABLE — MANUAL ONLY
Did the agent document every detail and select right call disposition?
- 1: Nothing documented
- 2: Didn't check previous posts, unclear/incorrect tagging
- 3: Documented somewhere but not everywhere, missed details
- 4: Documented in more than one place, concise summary
- 5: Concise summary + Slack post + Mission Control update
> NOTE: This requires cross-platform lookups (Slack, Mission Control, etc.).
> The AI never scores this. Leave as a manual field in all UI and output.

### 10. Customer Resolution Indicator (Y/N or N/A)
Did agent summarize result/actions AND ask if there's anything else they can do?
Binary check. Mark N/A if not applicable.

---

## Automatable vs Manual — Decision Matrix

| Section | AI Automatable? | Notes |
|---|---|---|
| Greeting | ✅ High confidence | Pattern match on first 10–15 sec of audio |
| Caller Identity Validation | ✅ High confidence | Binary Y/N detection |
| Purpose of the Call | ⚠️ Partial | AI detects probing; human reviews edge cases |
| Matching the Moment | ⚠️ Audio required | Tone/sentiment from audio; human review recommended |
| Process Adherence | ⚠️ Needs SOP/RAG | Accurate only with Notion SOP context injected |
| Call Resolution | ⚠️ Needs SOP/RAG | Same as above |
| Communication | ✅ High confidence | Language quality, slang, clarity from transcript |
| Efficiency & Call Handling | ✅ High confidence | Hold times, dead air, metadata from Dialpad |
| Documentation | ❌ Manual only | Cross-platform — never automate |
| Customer Resolution Indicator | ✅ High confidence | Binary Y/N, closing phrase detection |

---

## QA Sampling Strategy

### Why We Don't Score Every Call
At ~13,000 calls/month, scoring every call is operationally unrealistic and statistically unnecessary.
A well-designed sample delivers 95%+ confidence in conclusions at a fraction of the cost.
The VP goal should be reframed as: "statistically representative sample per agent, per week" — not full coverage.

### Sample Size
- **30–35 calls per agent per week** — roughly 10% of an agent's weekly call volume (~300 calls/week)
- Across a rolling 4-week window, this yields ~120 scored calls per agent — a robust basis for coaching and trend analysis

### Sampling Method: Stratified Random
Pure random sampling risks accidental clustering (e.g., all Monday morning calls for one agent).
Instead, sample randomly within these strata:

| Stratum | Why It Matters |
|---|---|
| Day of week | Behavior and volume differ across the week |
| Time of day | Morning vs. end-of-shift fatigue affects performance |
| Call duration | Short transactional calls vs. complex escalations |
| Call type | Billing vs. maintenance vs. move-in vs. general inquiry |
| Language | English vs. Spanish calls should both be represented |

### Call Length Cap
- **Calls over 25 minutes are flagged as "Manager review required"** — scored by AI with special attention to the "Efficiency" section of the scorecard
- These are typically the most complex, escalated calls — they warrant direct human review anyway
- The flag is automatic; the system surfaces them to a manager after AI scoring

### CSAT Is a Separate Pipeline
The current practice of selecting QA calls from low CSAT scores is **selection bias** — it
systematically over-samples bad calls and distorts the picture of each agent's real skill level.
Two distinct workflows must exist and never be mixed:

1. **Scheduled QA sample** — stratified random selection, runs weekly, primary performance metric
2. **Flagged call review** — triggered by low CSAT, complaints, or manager discretion, separate from QA sample

Coaching built only on flagged calls is reactive. Coaching built on the random sample is diagnostic.

### Database Implication
The database must store call metadata **before scoring happens** — from Phase 1 onward — to enable
the sampler. Required fields per call record:

| Field | Purpose |
|---|---|
| Agent ID | Link score to agent history |
| Call date & time | Stratified sampling across days/shifts |
| Call duration | Apply 25-min cap, stratify by length |
| Call type / disposition | Stratify by call category |
| Dialpad call ID | Unique identifier, links back to source recording |
| CSAT score | Available from Dialpad; used only in flagged pipeline |
| Language | English or Spanish — ensure both are sampled |
| Sampling status | `sampled` / `not_sampled` / `manual_only` |
| Scoring status | `pending` / `in_progress` / `complete` / `manual_only` |

---

## Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Primary audio AI | Gemini 2.5 Flash | Google ecosystem, native audio ingestion, cheapest at scale |
| Transcription fallback | OpenAI Whisper (open source) | High accuracy, no per-token cost, runs locally |
| Speaker diarization | pyannote.audio | Separates agent vs caller voice channels |
| Backend | FastAPI (Python) | Lightweight, async for batch processing, well-documented |
| SOP/RAG vector store | ChromaDB | Free, embeddable, no external service needed |
| Database | PostgreSQL | Via Railway or Render |
| Hosting | Railway or Render | GitHub-connected deploy, free tier available |
| Frontend (Phase 3) | Next.js or Retool | Retool = faster; Next.js = more control and skill-building |
| Existing flow | Google Sheets + Apps Script | Do NOT modify until Phase 3 |
| Embeddings (for RAG) | Google text-embedding or OpenAI | For chunking Notion SOPs |

---

## Reference GitHub Repos (Study These)

- **ReverendBayes/AI-Powered-Call-Center-Intelligence** — Closest to our Phase 1 target.
  FastAPI + Whisper + GPT + React/TypeScript. No Azure. Consider forking as starting point.
- **DrDroidLab/voicesummary** — FastAPI + React + PostgreSQL + AWS S3. Has pause detection
  and conversation health scoring — maps to our Efficiency and Matching the Moment sections.
- **alvaroarcelus/Sentiment-Analysis-Pipeline-for-Call-Center-Calls** — Demonstrates
  pyannote.audio + Whisper speaker diarization pipeline. Reference for separating agent/caller.

---

## Deployment Roadmap

### Phase 0 — Validation (Weeks 1–3) ← START HERE
**Goal:** Prove AI scoring quality before building anything.

- Pull 20-25 real calls from Dialpad (mp4 or mp3), calls must reflect a diversity of textbook great performances, clearly poor performances, Spanish calls and genuinely difficult calls with ambiguous resolutions.
- Write a standalone Python script that feeds each call to Gemini 2.5 Flash
- Output: a JSON scorecard per call (skip Documentation section)
- Sit with a Manager and compare AI scores to human scores
- Document where AI is accurate, where it needs SOP context, where human review is needed
- Go/no-go decision before writing any backend code

**Deliverables:** `score_call.py`, calibration notes, validated prompt templates per section.

### Phase 1 — Core Scoring Pipeline (Weeks 4–7)
**Goal:** Working backend that scores a call and writes draft to Google Sheets.

- FastAPI backend with 3 endpoints: upload audio, trigger scoring, return scorecard JSON
- Pipeline: audio file → Gemini 2.5 Flash → structured transcript → scoring prompt → JSON
- Write draft scorecard to Google Sheets via Sheets API
- Manager reviews draft in Sheets, approves → triggers existing Apps Script email flow
- Host on Railway or Render, deploy from GitHub
- **Database schema must include full call metadata from day one** (see QA Sampling Strategy above)
  — retrofitting metadata fields later is painful; design for the sampler even before it's built

**Deliverables:** Running API, draft score in Sheets after call upload, manager review column.

### Phase 2 — SOP/Notion RAG Integration (Weeks 8–12)
**Goal:** Accurate scoring of Process Adherence and Call Resolution.

- Connect Notion API → export SOP content nightly
- Chunk and embed SOPs using embedding model → store in ChromaDB
- At scoring time: identify call type from transcript → retrieve relevant SOP chunks → inject into scoring prompt
- Process Adherence and Call Resolution now score against actual policy, not general knowledge

**Deliverables:** Notion auto-sync job, dramatically improved accuracy on sections 5 and 6.

### Phase 3 — Manager Dashboard (Weeks 13–18)
**Goal:** Replace Sheets review workflow with a proper web interface.

- Next.js (preferred for skill-building) or Retool dashboard
- Features: upload call or pull from Dialpad, view AI draft scorecard with per-section confidence,
  manager approve/adjust/reject, approval triggers email, agent history view
- Google OAuth for auth (fits existing org)
- Deprecate Sheets draft workflow

**Deliverables:** Web app managers use daily. Apps Script email still fires on approval.

### Phase 4 — Dialpad Webhook Automation (Weeks 19–24)
**Goal:** Zero manual steps. Calls score themselves at end of call. 

- 30 - 35 calls per agent per week, randomly selected (statistically significant)
- Investigate Dialpad webhook for call completion events
- If available: auto-trigger scoring pipeline on call end
- Manager opens dashboard next morning to review overnight scores
- Human role becomes review-only, not upload-and-trigger

**Deliverables:** Fully automated end-to-end pipeline.

---

## Key Principles & Constraints

1. **Human review is non-negotiable at launch.** AI proposes scores; managers approve.
   Never let an AI score go directly to an agent without manager confirmation. Cultural risk.
2. **Documentation (Section 9) is always manual.** Never build automation for it.
3. **The existing Apps Script email flow is sacred until Phase 3.** Don't break it.
4. **Build for maintainability over cleverness.** This needs to outlast my time at Landing.
   Comment code, write READMEs, keep dependencies minimal.
5. **Start with one team's rubric and validate before expanding.**
6. **Dialpad already generates transcripts** — but we're not relying on them as our primary
   signal because they miss tone, emotion, and audio nuance. Audio is the source of truth.
7. **Cost matters at scale.** Gemini Flash at $0.40/million tokens vs GPT-4o at $15/million
   is a meaningful difference when processing hundreds of calls per week.

---

## What to Build First (Current Task)

When starting a session, ask me which phase we're working on. If I say Phase 0, start with:

```
score_call.py — A standalone Python script that:
1. Accepts a local audio file path as input
2. Uploads it to Gemini 2.5 Flash via the Google Generative AI SDK
3. Sends a structured scoring prompt based on the rubric above
4. Returns a JSON object with: section name, score (1-5 or Y/N), confidence (low/medium/high), and reasoning
5. Skips Documentation section entirely
6. Prints output to console and optionally saves to a .json file
```

Dependencies to use: `google-generativeai`, `python-dotenv`, `argparse`
API key stored in `.env` file as `GEMINI_API_KEY`.
