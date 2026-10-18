// Gemini standard-tier cost model, shared by the Cost Projection card and the
// per-agent card meta. Mirrors app/models.py — keep in sync when models change.

export const MODELS = ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.5-flash-lite"] as const;
export type ModelKey = (typeof MODELS)[number];

export const MODEL_LABELS: Record<ModelKey, string> = {
  "gemini-3.1-pro-preview": "3.1 Pro",
  "gemini-3.8-flash":       "3.8 Flash",
  "gemini-3.5-flash-lite":  "3.5 Flash-Lite",
};

// Maps route_extraction_strategy's tier name to the full Gemini model id it
// recommends — mirrors app/models.py's EXTRACTION_MODEL_TIERS, so the UI can
// show the actual model (e.g. "gemini-3.1-pro-preview") instead of just the
// tier name ("pro").
export const TIER_MODEL: Record<"lite" | "flash" | "pro", ModelKey> = {
  lite: "gemini-3.5-flash-lite",
  flash: "gemini-3.8-flash",
  pro: "gemini-3.1-pro-preview",
};

// Per 1M tokens (input, output), standard tier ≤200k prompt.
// Source: ai.google.dev/gemini-api/docs/pricing (2026-09-21). Introductory
// rate for gemini-3.8-flash; rises to $1.50/$7.50 on 2027-01-01 — revisit then.
export const PRICING: Record<ModelKey, { input: number; output: number }> = {
  "gemini-3.1-pro-preview": { input: 2.00, output: 12.00 },
  "gemini-3.8-flash":       { input: 0.75, output:  3.75 },
  "gemini-3.5-flash-lite":  { input: 0.30, output:  2.50 },
};

/** USD cost for a run. Returns 0 for an unknown model rather than NaN.
 *  thinkingTokens is billed at the same rate as outputTokens (Gemini reports
 *  it as a separate usage_metadata field, but there is no separate price). */
export function computeCost(
  model: string,
  inputTokens: number,
  outputTokens: number,
  thinkingTokens = 0,
): number {
  const p = PRICING[model as ModelKey];
  if (!p) return 0;
  return (inputTokens * p.input + (outputTokens + thinkingTokens) * p.output) / 1_000_000;
}

/** One-line "best for" blurb per model tier, shown in the cost projection tables. */
export const TIER_DESC: Record<string, string> = {
  "gemini-3.1-pro-preview": "Highest reasoning",
  "gemini-3.8-flash":       "Balanced",
  "gemini-3.5-flash-lite":  "Fastest and economical",
};

/** "$1,240" (>=1k, 0dp) or "$29.75" (<1k, 2dp). Caller appends the period suffix. */
export function fmtMoney(n: number): string {
  return n >= 1000
    ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
    : `$${n.toFixed(2)}`;
}

const CACHE_DISCOUNT = 0.90;

/** USD saved from Gemini implicit context cache hits (90% discount on cached input tokens). */
export function computeCacheSavings(model: string, cacheReadTokens: number): number {
  if (!cacheReadTokens) return 0;
  const p = PRICING[model as ModelKey];
  if (!p) return 0;
  return (cacheReadTokens * p.input * CACHE_DISCOUNT) / 1_000_000;
}
