// esbuild entry for tests/sop_deferred.test.mjs — exposes the worker's own
// prompt builders + trigger-time resolver (parity-harness pattern).
export {
  buildJudgePromptTemplate,
  buildJudgeSystemPrompt,
  buildScoringPrompt,
  buildSystemPrompt,
  sopBlockParts,
  renderSopContextBlock,
  SOP_BLOCK_PLACEHOLDER,
} from "../src/lib/scoringPrompts.js";
export { fetchSopContext, resolveDeferredSop } from "../src/lib/sopRetrieval.js";
