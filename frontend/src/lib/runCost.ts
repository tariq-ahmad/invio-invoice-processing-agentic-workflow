// Derives whole-run token/cost totals from the mirrored pipeline state, for the
// TopBar session meter and the approval card's cost panel. Reuses the same
// pricing model as the per-agent cards (app/lib/cost.ts) so they never diverge.

import type { PipelineState } from "../api/types";
import { computeCost, computeCacheSavings, MODEL_LABELS, type ModelKey } from "./cost";

export interface RunModelCost {
  model: string;
  label: string;
  inTokens: number;
  outTokens: number;
  cacheReadTokens: number;
  cacheSavingsUsd: number;
  cost: number;
}

/** One model call (or pass) in the run, for the per-step breakdown. */
export interface RunStepCost {
  key: string;
  label: string;
  model: string;
  modelLabel: string;
  inTokens: number;
  outTokens: number;
  cacheReadTokens: number;
  cost: number;
}

export interface RunCost {
  inTokens: number;
  outTokens: number;
  cacheReadTokens: number;
  cacheSavingsUsd: number;
  totalCost: number;
  models: RunModelCost[];
  steps: RunStepCost[];
}

const STEPS = [
  ["screening", "Screening"],
  ["extraction", "Extraction"],
  ["draft_email", "Rejection email draft"],
] as const;

/** The two approval-card briefings: the reviewer summary and the Agent Memory note.
 * Only counted when a model wrote them (the fallback text costs nothing). */
const CARD_CALLS = [
  ["review_summary", "Reviewer briefing"],
  ["memory_summary", "Agent Memory note"],
] as const;

export function deriveRunCost(state: PipelineState): RunCost {
  const byModel = new Map<string, RunModelCost>();
  const steps: RunStepCost[] = [];
  let inTokens = 0;
  let outTokens = 0;
  let cacheReadTokens = 0;
  let cacheSavingsUsd = 0;

  const add = (
    key: string,
    label: string,
    model: string,
    stepIn: number,
    stepOut: number,
    stepThink: number,
    stepCache: number,
    costUsd?: number,
  ) => {
    inTokens += stepIn;
    outTokens += stepOut + stepThink;
    cacheReadTokens += stepCache;

    const savings = computeCacheSavings(model, stepCache);
    cacheSavingsUsd += savings;
    const cost = costUsd != null ? costUsd : computeCost(model, stepIn, stepOut, stepThink) - savings;
    steps.push({
      key,
      label,
      model,
      modelLabel: MODEL_LABELS[model as ModelKey] ?? model,
      inTokens: stepIn,
      outTokens: stepOut + stepThink,
      cacheReadTokens: stepCache,
      cost,
    });
    const existing = byModel.get(model);
    if (existing) {
      existing.inTokens += stepIn;
      existing.outTokens += stepOut + stepThink;
      existing.cacheReadTokens += stepCache;
      existing.cacheSavingsUsd += savings;
      existing.cost += cost;
    } else {
      byModel.set(model, {
        model,
        label: MODEL_LABELS[model as ModelKey] ?? model,
        inTokens: stepIn,
        outTokens: stepOut + stepThink,
        cacheReadTokens: stepCache,
        cacheSavingsUsd: savings,
        cost,
      });
    }
  };

  for (const [step, label] of STEPS) {
    const model = state[`${step}_model`];
    if (!model) continue;
    // extraction's server-computed cost (app/nodes/extraction.py) prices the
    // main pass and the line-items fallback pass at THEIR OWN actual models
    // (tiering can pick lite/pro for the main call while the fallback always
    // bills at flash) after folding both passes' tokens into these same
    // state fields — recomputing from the folded tokens at this single
    // `model` would misprice whichever pass didn't use that rate, so prefer
    // the authoritative total when the backend provides it. No other step
    // currently has an equivalent `*_cost_usd` field.
    add(
      step,
      label,
      model,
      state[`${step}_input_tokens`] ?? 0,
      state[`${step}_output_tokens`] ?? 0,
      state[`${step}_thinking_tokens`] ?? 0,
      state[`${step}_cache_read_tokens`] ?? 0,
      step === "extraction" ? state.extraction_cost_usd : undefined,
    );
  }

  for (const [key, label] of CARD_CALLS) {
    const call = state[key];
    if (call?.source !== "llm" || !call.model) continue;
    add(
      key,
      label,
      call.model,
      call.input_tokens ?? 0,
      call.output_tokens ?? 0,
      call.thinking_tokens ?? 0,
      call.cache_read_tokens ?? 0,
    );
  }

  const models = [...byModel.values()].sort((a, b) => b.cost - a.cost);
  const totalCost = models.reduce((sum, m) => sum + m.cost, 0);

  return { inTokens, outTokens, cacheReadTokens, cacheSavingsUsd, totalCost, models, steps };
}
