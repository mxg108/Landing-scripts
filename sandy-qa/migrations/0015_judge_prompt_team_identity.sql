-- 0015_judge_prompt_team_identity.sql — judge calibration tweak 1 of 2
-- (owner-directed 2026-09-01). The MS system prompt opened with the generic
-- "for a call center at {company}"; sales_v2 and sofia_v0 already name
-- their own program in their templates, so only member_support_v2 changes —
-- first line now names the team. Tweak 2 (scoring anchors: "3 is
-- competent", "a 4 must name what was missing for the 5") is code-side in
-- JUDGE_GENERAL_INSTRUCTIONS (scoringPrompts.ts, v0.65) — the ungated
-- half of the judge system prompt. rubric_version stays member_support_v2:
-- the scoring_prompt block is prompt plumbing, not the scored-section
-- contract (rubric/formula changes proper remain management-gated).

UPDATE qa_rubric_versions
SET rubric_json = json_set(
  rubric_json,
  '$.scoring_prompt.system_prompt_template',
  'You are a QA evaluator for the Member Support call center at {company}.
You will listen to a full call recording and score it using the rubric provided.

CRITICAL OUTPUT RULES:
- Return ONLY a valid JSON object. No markdown fences, no prose, no explanations outside the JSON.
- All string values must have apostrophes and internal quotes escaped.
- Never use raw newlines inside string values — use a space instead.
- The JSON must be parseable by Python json.loads() without modification.
- Use the Second person ("you") when referring to the agent in the reasoning AND feedback sections, as if you are directly addressing them with a personal tone, DO NOT use the Third person ("they/them") anywhere in your outputs.'
)
WHERE rubric_version = 'member_support_v2';
