/**
 * "Cost projection" on the human-approval card — the same panel the Screening and
 * Extraction cards show (token usage, invoices-per-month slider, model comparison
 * table), but for the whole invoice: every model call made up to the decision,
 * summed. Derived from the token usage mirrored into pipeline state (lib/runCost.ts)
 * and priced with the same Gemini rates as the per-agent cards.
 */
import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { usePipelineStore } from "../store/usePipelineStore";
import { useThemeStore } from "../store/useThemeStore";
import { deriveRunCost } from "../lib/runCost";
import { computeCost, fmtMoney, MODELS, MODEL_LABELS, TIER_DESC } from "../lib/cost";
import { GeminiLogo } from "../components/common/GeminiLogo";
import { cn } from "../lib/utils";

const usd = (n: number) => `$${Math.max(0, n).toFixed(4)}`;

export function RunCostProjection() {
  const state = usePipelineStore((s) => s.state);
  const run = useMemo(() => deriveRunCost(state), [state]);
  const isDark = useThemeStore((s) => s.theme === "dark");
  const [open, setOpen] = useState(false);
  const [volume, setVolume] = useState(3_000);
  if (run.steps.length === 0) return null;

  // Output tokens already include thinking tokens here (lib/runCost.ts folds them).
  const { inTokens, outTokens, totalCost, cacheReadTokens, cacheSavingsUsd } = run;
  const rowBorder = isDark ? "1px solid rgba(255,255,255,0.05)" : "1px solid rgba(15,23,42,0.07)";
  const nameColor = isDark ? "#f1f5f9" : "#0f172a";
  const mutedColor = isDark ? "#94a3b8" : "#475569";
  const spendColor = isDark ? "#e2e8f0" : "#334155";
  const activeColor = isDark ? "var(--color-brand-300)" : "#2563eb";

  return (
    <div className="rounded-xl border border-line-strong bg-wash px-3.5 py-3">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 text-xs font-semibold text-slate-200 transition hover:text-slate-50"
      >
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          className={cn("transition-transform", open && "rotate-90")}
        >
          <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Cost projection
        <span className="font-normal text-slate-500">· overall, all steps so far</span>
        <span className="ml-auto font-mono text-[12px] font-semibold text-live-200">{usd(totalCost)}</span>
      </button>

      {open && (
        <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="mt-3 space-y-4">
          {/* Token usage that drives this invoice's cost */}
          <p className="font-mono text-xs text-slate-400">
            <span className="font-semibold text-slate-300">Tokens:</span>{" "}
            <span className="font-semibold text-slate-100">{inTokens.toLocaleString()}</span> in ·{" "}
            <span className="font-semibold text-slate-100">{outTokens.toLocaleString()}</span> out
            <span className="text-slate-500"> (incl. thinking)</span>
          </p>

          {/* What each call cost on this invoice */}
          <ul className="space-y-1">
            {run.steps.map((s) => (
              <li key={s.key} className="flex items-baseline gap-2 text-[11px]">
                <span className="min-w-0 flex-1 truncate text-slate-300">{s.label}</span>
                <span className="shrink-0 font-mono text-slate-500">
                  {s.modelLabel} · {s.inTokens.toLocaleString()} in · {s.outTokens.toLocaleString()} out
                </span>
                <span className="w-16 shrink-0 text-right font-mono text-slate-100 tabular-nums">{usd(s.cost)}</span>
              </li>
            ))}
            <li className="flex items-baseline gap-2 border-t border-line pt-1 text-[11px] font-semibold">
              <span className="flex-1 text-slate-200">This invoice so far</span>
              <span className="w-16 shrink-0 text-right font-mono text-live-200 tabular-nums">{usd(totalCost)}</span>
            </li>
          </ul>
          {cacheReadTokens > 0 && (
            <p className="text-[11px] text-slate-500">
              <span className="text-teal-400">{cacheReadTokens.toLocaleString()} tokens</span> served from Gemini
              implicit context cache, billed at 10% of the standard input rate · saving ~
              {usd(cacheSavingsUsd)} on this invoice.
            </p>
          )}

          {/* Monthly volume slider */}
          <div>
            <div className="flex items-baseline justify-between">
              <span className="text-[11px] uppercase tracking-wider text-slate-400">Invoices / month</span>
              <span className="font-mono text-sm font-semibold tabular-nums text-slate-100">
                {volume.toLocaleString()}
              </span>
            </div>
            <input
              type="range"
              min={1000}
              max={50000}
              step={1000}
              value={volume}
              onChange={(e) => setVolume(Number(e.target.value))}
              className="mt-1.5 w-full accent-brand-500"
            />
          </div>

          {/* Projection: the current mix of models, then every step on one tier */}
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                {["MODEL", "BEST FOR", "MONTHLY SPEND", "STATUS"].map((h, i) => (
                  <th
                    key={h}
                    style={{
                      textAlign: i >= 2 ? "right" : "left",
                      padding: "0 8px 8px",
                      fontSize: 10,
                      fontWeight: 500,
                      letterSpacing: "0.08em",
                      color: "var(--color-slate-500)",
                      borderBottom: isDark ? "1px solid rgba(255,255,255,0.08)" : "1px solid rgba(15,23,42,0.09)",
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: rowBorder }}>
                <td style={{ padding: "10px 8px", whiteSpace: "nowrap" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", fontWeight: 700, fontSize: 13, color: nameColor }}>
                    <GeminiLogo />
                    Current setup
                  </span>
                </td>
                <td style={{ padding: "10px 8px", fontSize: 13, color: mutedColor }}>Each step on its own model</td>
                <td style={{ padding: "10px 8px", textAlign: "right", fontFamily: "monospace", fontSize: 14, fontWeight: 600, color: activeColor }}>
                  {fmtMoney(totalCost * volume)}
                  <span style={{ fontSize: 10, fontWeight: 400, color: "var(--color-slate-500)" }}>/mo</span>
                </td>
                <td style={{ padding: "10px 8px", textAlign: "right" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5, background: "color-mix(in srgb, var(--color-brand-400) 14%, transparent)", color: "var(--color-brand-300)", borderRadius: 999, padding: "2px 10px", fontSize: 11, fontWeight: 500 }}>
                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--color-brand-300)", display: "inline-block" }} />
                    Active
                  </span>
                </td>
              </tr>
              {MODELS.map((m) => (
                <tr key={m} style={{ borderBottom: rowBorder }}>
                  <td style={{ padding: "10px 8px", whiteSpace: "nowrap" }}>
                    <span style={{ display: "inline-flex", alignItems: "center", fontWeight: 700, fontSize: 13, color: nameColor }}>
                      <GeminiLogo />
                      All steps on {MODEL_LABELS[m]}
                    </span>
                  </td>
                  <td style={{ padding: "10px 8px", fontSize: 13, color: mutedColor }}>{TIER_DESC[m]}</td>
                  <td style={{ padding: "10px 8px", textAlign: "right", fontFamily: "monospace", fontSize: 14, fontWeight: 600, color: spendColor }}>
                    {fmtMoney(computeCost(m, inTokens, outTokens) * volume)}
                    <span style={{ fontSize: 10, fontWeight: 400, color: "var(--color-slate-500)" }}>/mo</span>
                  </td>
                  <td style={{ padding: "10px 8px" }} />
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[11px] leading-snug text-slate-500">
            Projection assumes every invoice costs about what this one did. "Current setup" is actual spend (after
            caching); the "All steps on…" rows are list-price estimates with no caching. Standard Gemini pricing;
            excludes Model Armor and hosting.
          </p>
        </motion.div>
      )}
    </div>
  );
}
