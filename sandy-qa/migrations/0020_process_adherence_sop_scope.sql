-- 0020_process_adherence_sop_scope.sql — judge calibration: scope Process
-- Adherence to the retrieved SOP documents (owner-directed 2026-09-17).
--
-- Observed drift (D1 evals 10001223–10001232, all sop_used): the judge was
-- re-grading the whole call under Section 5 — hold etiquette, redundant
-- clarifying questions, closing confirmations, pacing — i.e. the ground
-- that Sections 6/7/8 (Call Resolution, Communication, Efficiency) own.
-- Doctrine: Process Adherence is scoped EXCLUSIVELY to adherence to the
-- SOP documents retrieved for the call (the SOP CONTEXT block). The rest
-- of the rubric exists to score the rest of the call. Call Resolution is
-- untouched — it scores the OUTCOME, not the process — and stays in
-- scoring_prompt.sop_sections so the SOP block still informs it.
--
-- Data-only (rubric_question + special_reasoning_instructions on the
-- process_adherence section of member_support_v2); no app deploy needed —
-- buildScoringRubric renders rubric_question verbatim under the section
-- header and buildOutputSchema folds special_reasoning_instructions into
-- the reasoning hint, so the next enqueue picks it up. Score anchors,
-- range, weights and rubric_version are unchanged (same stance as 0015:
-- the scored-section contract — scale/weights — is what a version bump
-- gates; this is a scoping clarification of the section's intent).
--
-- Rollback: json_set the two keys back to
--   rubric_question = 'Did the agent follow correct process and company policies?'
--   special_reasoning_instructions = null

UPDATE qa_rubric_versions
SET rubric_json = json_set(
  rubric_json,
  '$.sections[' || (
    SELECT key FROM json_each(qa_rubric_versions.rubric_json, '$.sections')
    WHERE json_extract(value, '$.id') = 'process_adherence'
  ) || '].rubric_question',
  'Did the agent follow the procedure laid out in the SOP documents retrieved for this call (the SOP CONTEXT block)?
SCOPE — this section is scoped EXCLUSIVELY to adherence to the retrieved SOP documents. Score ONLY the steps, rules and prohibitions those documents state, and only where they apply to this call''s situation. Do NOT re-score the overall call-handling flow here: greeting, identity verification, purpose discovery, tone, communication, hold etiquette, pacing, resolution quality and next steps are owned by their own sections and must not raise or lower this score. If the retrieved SOP does not address something the agent did, it is out of scope for this section. If no SOP CONTEXT block is present, follow the SOP-missing note and score conservatively — do not substitute general process judgement.',
  '$.sections[' || (
    SELECT key FROM json_each(qa_rubric_versions.rubric_json, '$.sections')
    WHERE json_extract(value, '$.id') = 'process_adherence'
  ) || '].special_reasoning_instructions',
  'naming the SOP document and the specific step(s) or rule(s) the score rests on when SOP context was provided'
)
WHERE rubric_version = 'member_support_v2';
