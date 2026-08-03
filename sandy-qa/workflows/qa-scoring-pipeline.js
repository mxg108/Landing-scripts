/**
 * qa-scoring-pipeline — the QA scoring engine's AI legs (PortManifest §6).
 *
 * Design split: the qa-scoring APP builds every prompt (rubric from D1,
 * SOP block, CC disposition grounding, long-call notes) and passes them in
 * the trigger payload; this workflow is a GENERIC durable executor:
 *
 *   audio-leg   Dialpad meta → recording download (in-memory) → Gemini
 *               Files upload → ACTIVE poll → Stage-A annotate (constrained
 *               schema, bounded thinking, 0→0.2 temp-bump retry)
 *   judge-leg   Stage-B judge from the annotation — Claude via the AI
 *               Gateway anthropic passthrough (pinned model) or Gemini
 *               direct, payload-driven
 *   plan-b      single-stage Gemini audio scoring when annotate or judge
 *               fails (payload carries the full single-stage prompt)
 *   callback    scorecard JSON + provenance + timings back to the app,
 *               which validates, computes the overall score, and persists
 *
 * Spike-proven constraints honored: whole audio leg in ONE step (step
 * returns are checkpointed + size-capped); thinkingConfig.thinkingBudget
 * carried (thinking eats maxOutputTokens on gemini-2.5); judge model is
 * PINNED, never dynamic/sandy-workflows.
 *
 * Secrets (per-workflow): DIALPAD_API_KEY, GEMINI_API_KEY. Org: AI_GATEWAY_TOKEN.
 *
 * Payload:
 * {
 *   call_id, team_id,
 *   pipeline: "two_stage" | "single",
 *   annotator: { model, system, prompt, response_schema, thinking_budget,
 *                max_output_tokens },
 *   judge: { provider: "anthropic"|"gemini", model, system,
 *            prompt_template,           // "{{ANNOTATION_JSON}}" placeholder
 *            max_tokens },
 *   single_stage: { model, system, prompt, temperature, max_output_tokens },
 *   callback_url, callback_token
 * }
 */

import { WorkflowEntrypoint } from "cloudflare:workers";

const GEMINI = "https://generativelanguage.googleapis.com";
const AIG_ANTHROPIC =
  "https://gateway.ai.cloudflare.com/v1/d094105625bec434114c0b80ecfa7238/sandy-workflows/anthropic/v1/messages";

const RETRY = { retries: { limit: 1, delay: "10 seconds", backoff: "linear" } };

async function sendCallback(callbackUrl, payload, sandySecrets) {
  const secret = sandySecrets?._sandyCallbackSecret;
  const bodyText = JSON.stringify(payload);
  const headers = { "Content-Type": "application/json" };
  if (secret) {
    const pathname = new URL(callbackUrl).pathname;
    const key = await crypto.subtle.importKey(
      "raw", new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const mac = await crypto.subtle.sign(
      "HMAC", key, new TextEncoder().encode(`POST|${pathname}|${bodyText}`));
    headers["X-Sandy-Workflow-Callback"] = Array.from(new Uint8Array(mac))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const res = await fetch(callbackUrl, {
    method: "POST", headers, body: bodyText,
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`Callback failed: HTTP ${res.status}`);
}

function stripFences(text) {
  return String(text)
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/, "")
    .trim();
}

// Extract the first complete JSON object from model text (fence-tolerant).
function extractJson(text) {
  const t = stripFences(text);
  const start = t.indexOf("{");
  const end = t.lastIndexOf("}") + 1;
  if (start === -1 || end === 0) throw new Error("no JSON object in response");
  let body = t.slice(start, end).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, "");
  try {
    return JSON.parse(body);
  } catch (first) {
    // trailing-comma salvage (observed live on a judge output, eval 2444)
    const fixed = body.replace(/,(\s*[}\]])/g, "$1");
    try {
      return JSON.parse(fixed);
    } catch {
      throw new Error(`JSON parse failed: ${String(first).slice(0, 120)}`);
    }
  }
}

function finishDiag(genJson) {
  const cand = (genJson.candidates ?? [])[0] ?? {};
  const usage = genJson.usageMetadata ?? {};
  return {
    finish_reason: cand.finishReason ?? null,
    prompt_tokens: usage.promptTokenCount ?? null,
    output_tokens: usage.candidatesTokenCount ?? null,
    thoughts_tokens: usage.thoughtsTokenCount ?? null,
  };
}

async function geminiGenerate(gmKey, model, parts, genConfig, system) {
  const res = await fetch(`${GEMINI}/v1beta/models/${model}:generateContent`, {
    method: "POST",
    headers: { "x-goog-api-key": gmKey, "content-type": "application/json" },
    body: JSON.stringify({
      contents: [{ role: "user", parts }],
      systemInstruction: system
        ? { parts: [{ text: system }] }
        : undefined,
      generationConfig: genConfig,
    }),
    signal: AbortSignal.timeout(480000),
  });
  if (!res.ok)
    throw new Error(`gemini ${model} HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const json = await res.json();
  const text = ((json.candidates?.[0]?.content ?? {}).parts ?? [])
    .map((p) => p.text ?? "")
    .join("");
  return { json, text, diag: finishDiag(json) };
}

export class TenantWorkflow extends WorkflowEntrypoint {
  async run(event, step) {
    const p = event.payload;
    const dpKey = p._sandySecrets?.DIALPAD_API_KEY;
    const gmKey = p._sandySecrets?.GEMINI_API_KEY;
    const aigKey = p._sandySecrets?.AI_GATEWAY_TOKEN;

    const result = {
      call_id: p.call_id,
      team_id: p.team_id,
      status: "error",
      pipeline_requested: p.pipeline,
      scorer_provider: null,
      scorer_model: null,
      pipeline_fallback_reason: null,
      annotation: null,
      annotator_model: null,
      annotate_diag: null,
      scorecard_raw: null,
      timings_ms: {},
      error: null,
    };

    let audioLeg = null;
    try {
      if (!p.call_id || !dpKey || !gmKey)
        throw new Error("missing call_id or DIALPAD/GEMINI secrets");

      // ── audio leg: download + upload + Stage-A annotate (ONE step) ────────
      audioLeg = await step.do("audio-leg", RETRY, async () => {
        const out = { timings: {} };
        let t = Date.now();
        const metaRes = await fetch(
          `https://dialpad.com/api/v2/call/${encodeURIComponent(p.call_id)}`,
          { headers: { authorization: `Bearer ${dpKey}` } });
        if (!metaRes.ok)
          throw new Error(`dialpad-meta HTTP ${metaRes.status}`);
        const meta = await metaRes.json();
        const rec = (meta.recording_details ?? [])[0] ?? {};
        if (!rec.url) throw new Error("no recording_details url");
        const recUrl = new URL(rec.url);
        recUrl.searchParams.set("apikey", dpKey);
        const audioRes = await fetch(recUrl.toString(), { redirect: "follow" });
        if (!audioRes.ok) throw new Error(`dialpad-audio HTTP ${audioRes.status}`);
        const audioBuf = await audioRes.arrayBuffer();
        const mime = (audioRes.headers.get("content-type") ?? "audio/mp3").split(";")[0].trim();
        out.audio_bytes = audioBuf.byteLength;
        out.timings.download = Date.now() - t;

        // Gemini Files resumable upload
        t = Date.now();
        const startRes = await fetch(`${GEMINI}/upload/v1beta/files`, {
          method: "POST",
          headers: {
            "x-goog-api-key": gmKey,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": String(audioBuf.byteLength),
            "X-Goog-Upload-Header-Content-Type": mime,
            "content-type": "application/json",
          },
          body: JSON.stringify({ file: { display_name: `score-${p.call_id}` } }),
        });
        const uploadUrl = startRes.headers.get("x-goog-upload-url");
        if (!startRes.ok || !uploadUrl)
          throw new Error(`gemini-upload-start HTTP ${startRes.status}`);
        const upRes = await fetch(uploadUrl, {
          method: "POST",
          headers: {
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "content-type": mime,
          },
          body: audioBuf,
        });
        if (!upRes.ok) throw new Error(`gemini-upload HTTP ${upRes.status}`);
        const file = ((await upRes.json()).file ?? {});
        let state = file.state ?? "PROCESSING";
        for (let i = 0; state !== "ACTIVE" && i < 30; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          const pr = await fetch(`${GEMINI}/v1beta/${file.name}`, {
            headers: { "x-goog-api-key": gmKey } });
          state = (await pr.json()).state;
          if (state === "FAILED") throw new Error("gemini file FAILED");
        }
        if (state !== "ACTIVE") throw new Error("gemini file never ACTIVE");
        out.gemini_file = { name: file.name, uri: file.uri, mime };
        out.timings.upload = Date.now() - t;

        // Stage-A annotation with temp-bump retry (0 → 0.2); constrained
        // decoding via payload schema; bounded thinking.
        if (p.pipeline !== "single" && p.annotator) {
          t = Date.now();
          const filePart = { fileData: { fileUri: file.uri, mimeType: mime } };
          let lastErr = null;
          for (const temperature of [0.0, 0.2]) {
            try {
              const { json, text, diag } = await geminiGenerate(
                gmKey, p.annotator.model,
                [filePart, { text: p.annotator.prompt }],
                {
                  temperature,
                  maxOutputTokens: p.annotator.max_output_tokens ?? 65536,
                  thinkingConfig: { thinkingBudget: p.annotator.thinking_budget ?? 4096 },
                  responseMimeType: "application/json",
                  responseSchema: p.annotator.response_schema,
                },
                p.annotator.system
              );
              out.annotate_diag = diag;
              if ((diag.finish_reason ?? "").includes("MAX_TOKENS"))
                throw new Error(`annotation truncated [${JSON.stringify(diag)}]`);
              out.annotation = JSON.parse(text); // schema-forced → parseable
              break;
            } catch (e) {
              lastErr = e;
            }
          }
          if (!out.annotation) out.annotate_error = String(lastErr).slice(0, 300);
          out.timings.annotate = Date.now() - t;
        }
        return out;
      });
      result.timings_ms = audioLeg.timings;
      result.annotation = audioLeg.annotation ?? null;
      result.annotator_model = audioLeg.annotation ? p.annotator?.model : null;
      result.annotate_diag = audioLeg.annotate_diag ?? null;

      // ── judge leg (two_stage) ─────────────────────────────────────────────
      if (p.pipeline === "two_stage" && result.annotation && p.judge) {
        try {
          const judged = await step.do("judge-leg", RETRY, async () => {
            const t = Date.now();
            const prompt = p.judge.prompt_template.replace(
              "{{ANNOTATION_JSON}}", JSON.stringify(result.annotation));
            let text;
            if (p.judge.provider === "anthropic") {
              if (!aigKey) throw new Error("AI_GATEWAY_TOKEN missing");
              const res = await fetch(AIG_ANTHROPIC, {
                method: "POST",
                headers: {
                  "cf-aig-authorization": aigKey,
                  "anthropic-version": "2023-06-01",
                  "content-type": "application/json",
                },
                body: JSON.stringify({
                  model: p.judge.model, // pinned — never dynamic/
                  max_tokens: p.judge.max_tokens ?? 16384,
                  system: p.judge.system,
                  messages: [{ role: "user", content: prompt }],
                }),
                signal: AbortSignal.timeout(480000),
              });
              if (!res.ok)
                throw new Error(`gateway ${res.status}: ${(await res.text()).slice(0, 200)}`);
              text = ((await res.json()).content ?? [])
                .map((b) => b.text ?? "").join("");
            } else {
              const g = await geminiGenerate(
                gmKey, p.judge.model, [{ text: prompt }],
                { temperature: 0.2, maxOutputTokens: p.judge.max_tokens ?? 16384 },
                p.judge.system);
              text = g.text;
            }
            return { scorecard: extractJson(text), ms: Date.now() - t };
          });
          result.scorecard_raw = judged.scorecard;
          result.scorer_provider = p.judge.provider;
          result.scorer_model = p.judge.model;
          result.timings_ms.judge = judged.ms;
        } catch (e) {
          result.pipeline_fallback_reason = "text_scorer_failed";
        }
      } else if (p.pipeline === "two_stage" && !result.annotation) {
        result.pipeline_fallback_reason = "annotate_failed";
      }

      // ── Plan B: single-stage audio scoring ────────────────────────────────
      if (!result.scorecard_raw && p.single_stage) {
        const single = await step.do("single-stage", RETRY, async () => {
          const t = Date.now();
          const g = await geminiGenerate(
            gmKey, p.single_stage.model,
            [
              { fileData: { fileUri: audioLeg.gemini_file.uri, mimeType: audioLeg.gemini_file.mime } },
              { text: p.single_stage.prompt },
            ],
            {
              temperature: p.single_stage.temperature ?? 0.2,
              maxOutputTokens: p.single_stage.max_output_tokens ?? 65536,
            },
            p.single_stage.system);
          return { scorecard: extractJson(g.text), diag: g.diag, ms: Date.now() - t };
        });
        result.scorecard_raw = single.scorecard;
        result.scorer_provider = "gemini";
        result.scorer_model = p.single_stage.model;
        result.timings_ms.single_stage = single.ms;
      }

      if (!result.scorecard_raw) throw new Error("no scorecard produced by any path");
      result.status = "complete";
    } catch (err) {
      result.error = String(err && err.message ? err.message : err).slice(0, 500);
    }

    // ── cleanup + callback ──────────────────────────────────────────────────
    await step.do("finish", async () => {
      if (audioLeg?.gemini_file?.name) {
        try {
          await fetch(`${GEMINI}/v1beta/${audioLeg.gemini_file.name}`, {
            method: "DELETE",
            headers: { "x-goog-api-key": gmKey },
          });
        } catch {}
      }
      if (p.callback_url) {
        await sendCallback(
          p.callback_url,
          { run_id: event.instanceId, callback_token: p.callback_token, ...result },
          p._sandySecrets
        );
      }
      return { delivered: !!p.callback_url, status: result.status };
    });

    if (result.status !== "complete") throw new Error(result.error ?? "pipeline failed");
    return { status: result.status, call_id: p.call_id };
  }
}
