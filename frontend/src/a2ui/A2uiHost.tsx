import { useState, useEffect, useCallback } from "react";
import { motion } from "framer-motion";
import { usePipelineStore } from "../store/usePipelineStore";
import { useThemeStore } from "../store/useThemeStore";
import { useWorkingLabel } from "../hooks/useWorkingLabel";
import type {
  FieldValue,
  InvoiceRecord,
  InvoiceScreeningAssessment,
  SignalStrength,
  SourceGrounding,
} from "../api/types";
import { cn } from "../lib/utils";
import { cardReveal, staggerParent, staggerItem } from "../lib/motion";
import { CountUp } from "../components/common/CountUp";
import { AgentGlyph } from "../components/common/AgentGlyph";
import { StepHeader } from "../components/common/StepHeader";
import { computeCost, computeCacheSavings, fmtMoney, MODELS, MODEL_LABELS, TIER_DESC } from "../lib/cost";
import { GeminiLogo } from "../components/common/GeminiLogo";
import { requiredFieldsGrounded } from "../lib/policy";
import { ApprovalReview } from "./ApprovalReview";
import { STAGES } from "../store/stageMap";
import { stageForSurface } from "../lib/stageCards";
import { useReplay } from "../replay/controller";

/** "1.8s" for >=1s, else "940ms". */
function fmtLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/** Model · latency · token usage · $ cost tag for agent-produced surfaces. */
function ModelTag({
  model,
  ms,
  inTok,
  outTok,
  thinkTok,
  thinkLevel,
  costUsd,
  cacheReadTokens,
}: {
  model?: string;
  ms?: number;
  inTok?: number;
  outTok?: number;
  thinkTok?: number;
  /** Reasoning depth applied to this call (MINIMAL/LOW/MEDIUM/HIGH) — only
   * known for extraction, whose depth is decided per-invoice by
   * route_extraction_strategy (see app/nodes/extraction.py). */
  thinkLevel?: string;
  /** Server-computed real spend, blended across each underlying call's
   * ACTUAL model (e.g. extraction's tiered main call + its flash-only
   * line-items pass). When provided, takes precedence over the client-side
   * computeCost(model, ...) below, which would otherwise price the whole
   * folded token total at this single `model`'s rate. */
  costUsd?: number;
  /** Tokens served from Gemini implicit context cache for this call. */
  cacheReadTokens?: number;
}) {
  if (!model) return null;
  const hasTokens = inTok != null && outTok != null;
  return (
    <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1 rounded-lg border border-line bg-wash px-2.5 py-1.5 font-mono text-[12px] text-slate-300">
      <AgentGlyph size={16} />
      <span className="rounded-md border border-brand-400/40 bg-brand-500/20 px-2 py-0.5 font-semibold text-brand-200">{model}</span>
      {thinkLevel && (
        <span
          className="rounded-md border border-amber-400/30 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-300/90"
          title="Reasoning depth (thinking_level) applied to this call"
        >
          {thinkLevel} thinking
        </span>
      )}
      {!!cacheReadTokens && !!inTok && (
        <span
          className="rounded-md border border-teal-400/30 bg-teal-500/10 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-teal-300/90"
          title={`${cacheReadTokens.toLocaleString()} of ${inTok.toLocaleString()} input tokens (${Math.round((cacheReadTokens / inTok) * 100)}%) served from Gemini implicit context cache — billed at 10% of the standard input rate`}
        >
          {cacheReadTokens.toLocaleString()} tkns cached
        </span>
      )}
      {ms != null && (
        <>
          <span className="text-slate-500">·</span>
          <span className="font-medium text-live-300">{fmtLatency(ms)}</span>
        </>
      )}
      {(costUsd != null || hasTokens) && (
        <>
          <span className="text-slate-500">·</span>
          <span className="font-medium text-live-200">
            ${(costUsd ?? computeCost(model, inTok!, outTok!, thinkTok ?? 0)).toFixed(6)}
          </span>
        </>
      )}
      {/* Thinking-token count intentionally omitted here — it's shown in the
          Cost projection panel. thinkTok still feeds the $ cost above. */}
    </div>
  );
}

/** Collapsible per-agent cost panel: token usage, monthly-volume slider, and a
    3-model comparison table (Pro / Flash / Flash-Lite). Reuses lib/cost. */
function AgentCostProjection({
  model,
  inTok,
  outTok,
  thinkTok,
  thinkLevel,
  cacheReadTokens,
}: {
  model?: string;
  inTok?: number;
  outTok?: number;
  thinkTok?: number;
  thinkLevel?: string;
  cacheReadTokens?: number;
}) {
  const [open, setOpen] = useState(false);
  const [volume, setVolume] = useState(3_000);
  const isDark = useThemeStore((s) => s.theme === "dark");
  if (inTok == null || outTok == null) return null;
  const think = thinkTok ?? 0;

  return (
    <div className="border-t border-line pt-2">
      <button
        onClick={() => setOpen((v) => !v)}
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
        <span className="text-slate-500">· model tiering</span>
      </button>

      {open && (
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          className="mt-3 space-y-4"
        >
          {/* Token usage that drives this step's cost */}
          <p className="font-mono text-xs text-slate-400">
            <span className="font-semibold text-slate-300">Tokens:</span>{" "}
            <span className="font-semibold text-slate-100">{inTok.toLocaleString()}</span> in ·{" "}
            <span className="font-semibold text-slate-100">{outTok.toLocaleString()}</span> out
            {!!think && (
              <>
                {" "}+{" "}
                <span className="font-semibold text-amber-300/90">{think.toLocaleString()}</span>{" "}
                thinking
              </>
            )}
          </p>
          {!!think && (
            <p className="text-[11px] text-slate-500">
              Thinking tokens are billed at the same rate as output tokens — included in the spend below.
              {thinkLevel && (
                <>
                  {" "}Reasoning depth: <span className="font-semibold text-slate-300">{thinkLevel}</span>.
                </>
              )}
            </p>
          )}
          {!!cacheReadTokens && (
            <p className="text-[11px] text-slate-500">
              <span className="text-teal-400">{cacheReadTokens.toLocaleString()} tokens</span>
              {" "}({Math.round((cacheReadTokens / inTok) * 100)}% of input) served from Gemini implicit context cache
              — billed at 10% of the standard input rate
              {model && ` · saving ~$${computeCacheSavings(model, cacheReadTokens).toFixed(4)} this call`}.
              {" "}Cache applies to input tokens only — thinking and output tokens are billed at the full rate.
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

          {/* Model tier comparison table */}
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
              {MODELS.map((m) => {
                const perMonth = computeCost(m, inTok, outTok, think) * volume;
                const isActive = m === model;
                return (
                  <tr key={m} style={{ borderBottom: isDark ? "1px solid rgba(255,255,255,0.05)" : "1px solid rgba(15,23,42,0.07)" }}>
                    <td style={{ padding: "10px 8px", whiteSpace: "nowrap" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", fontWeight: 700, fontSize: 13, color: isDark ? "#f1f5f9" : "#0f172a" }}>
                        <GeminiLogo />
                        Gemini {MODEL_LABELS[m]}
                      </span>
                    </td>
                    <td style={{ padding: "10px 8px", fontSize: 13, color: isDark ? "#94a3b8" : "#475569" }}>
                      {TIER_DESC[m]}
                    </td>
                    <td style={{ padding: "10px 8px", textAlign: "right", fontFamily: "monospace", fontSize: 14, fontWeight: 600, color: isActive ? (isDark ? "var(--color-brand-300)" : "#2563eb") : (isDark ? "#e2e8f0" : "#334155") }}>
                      {fmtMoney(perMonth)}
                      <span style={{ fontSize: 10, fontWeight: 400, color: "var(--color-slate-500)" }}>/mo</span>
                    </td>
                    <td style={{ padding: "10px 8px", textAlign: "right" }}>
                      {isActive && (
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 5, background: "color-mix(in srgb, var(--color-brand-400) 14%, transparent)", color: "var(--color-brand-300)", borderRadius: 999, padding: "2px 10px", fontSize: 11, fontWeight: 500 }}>
                          <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--color-brand-300)", display: "inline-block" }} />
                          Active
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </motion.div>
      )}
    </div>
  );
}

function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cn("h-4 w-4 animate-spin", className)} viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.3" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

function CheckMini({ className }: { className?: string }) {
  return (
    <svg className={cn("h-4 w-4", className)} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 13l4 4L19 7" />
    </svg>
  );
}

/**
 * Renders an agent-emitted A2UI surface. The agent emits A2UI protocol messages
 * (consumed by the store's MessageProcessor), but for rendering reliability the
 * display is native React driven by the same structured pipeline state — so the
 * interactive controls always render regardless of the host browser.
 */
// Every A2UI surface maps to exactly one pipeline stage, driving its step
// number/badge/narrative from the same STAGES source of truth used by the
// chat bubbles and the timeline — no more separately-maintained card titles.
export function A2uiHost({ surfaceId }: { surfaceId: string }) {
  const stageId = stageForSurface(surfaceId);
  const stepDef = STAGES[stageId];
  const step = usePipelineStore((s) => s.stepNumbers[stageId]);
  // The slow accent sweep marks the one card that is waiting on the user.
  const awaitingYou = usePipelineStore((s) => {
    if (s.status !== "awaiting_input") return false;
    const emailTurn = s.currentInterrupt?.kind === "email_review";
    return surfaceId.startsWith("email_review")
      ? emailTurn
      : surfaceId.startsWith("approval_gate") && !emailTurn;
  });

  const meta = surfaceId.startsWith("invoice_screening")
    ? {
        accent: "from-teal-400/80 via-brand-400/50 to-brand-600/30",
        chip: "bg-teal-400/15 text-teal-200",
      }
    : surfaceId.startsWith("agent_memory")
      ? {
          accent: "from-violet-400/80 via-violet-500/50 to-violet-600/30",
          chip: "bg-violet-400/15 text-violet-200",
        }
      : surfaceId.startsWith("approval_gate")
        ? {
            accent: "from-amber-300/80 via-amber-400/50 to-amber-500/30",
            chip: "bg-amber-400/15 text-amber-200",
          }
        : surfaceId.startsWith("email_review")
          ? {
              accent: "from-rose-400/70 via-rose-500/40 to-accent-500/40",
              chip: "bg-rose-400/15 text-rose-200",
            }
          : {
              accent: "from-brand-400 to-brand-600",
              chip: "bg-gradient-to-r from-brand-500/20 to-accent-500/20 text-brand-200",
            };

  return (
    <motion.div
      variants={cardReveal}
      initial="initial"
      animate="animate"
      exit="exit"
      // `shrink-0` is REQUIRED: this card uses `overflow-hidden` (to clip the
      // rounded top accent bar), and per CSS an overflow!=visible flex item gets
      // `min-height: 0`. Inside the conversation pane's `flex flex-col
      // overflow-y-auto` column, that let flexbox CRUSH the card to ~2px high
      // (collapsing the whole surface — extraction card / approval gate buttons)
      // whenever the column's content exceeded its height, instead of scrolling.
      // The plain chat bubbles survive because they keep `overflow: visible`
      // (min-height: auto). Pinning flex-shrink to 0 makes the column scroll.
      className={cn("card shrink-0 overflow-hidden rounded-2xl", awaitingYou && "gradient-border is-live")}
    >
      <div className={`h-[3px] w-full bg-gradient-to-r ${meta.accent}`} />
      <div className="flex items-start gap-2 border-b border-line px-4 py-2.5">
        {surfaceId.startsWith("approval_gate") && (
          <span className="mt-0.5 shrink-0 text-amber-300">
            <UserIcon />
          </span>
        )}
        <StepHeader step={step} title={stepDef.label} agent={!!stepDef.agent} narrative={stepDef.narrative} />
        <span className={cn("ml-auto shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider", meta.chip)}>
          A2UI
        </span>
      </div>
      <div className="p-4 text-slate-200">
        {surfaceId.startsWith("invoice_screening") ? (
          <ScreeningCard />
        ) : surfaceId.startsWith("agent_memory") ? (
          <MemoryCard />
        ) : surfaceId.startsWith("approval_gate") ? (
          <ApprovalReview />
        ) : surfaceId.startsWith("email_review") ? (
          <EmailEditor />
        ) : (
          <ExtractionCard />
        )}
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Confidence badge
// ---------------------------------------------------------------------------

const GROUNDING_LABEL: Record<SourceGrounding, string> = {
  grounded: "Grounded",
  inferred: "Inferred",
  not_found: "Not found",
};

function GroundingBadge({ value }: { value?: SourceGrounding }) {
  const g = value ?? "not_found";
  const cls =
    g === "grounded"
      ? "text-live-400 bg-live-500/10 border-live-400/30"
      : g === "inferred"
        ? "text-amber-300 bg-amber-400/10 border-amber-400/30"
        : "text-rose-300 bg-rose-400/10 border-rose-400/30";
  return (
    <span className={cn("rounded-full border px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide", cls)}>
      {GROUNDING_LABEL[g]}
    </span>
  );
}

const fv = (f?: FieldValue) => (f?.value && String(f.value).trim() ? String(f.value) : "—");

// ---------------------------------------------------------------------------
// JSON syntax highlighting + fullscreen modal
// ---------------------------------------------------------------------------

function colorizeJson(json: string): React.ReactNode[] {
  const TOKEN = /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|[{}[\],])/g;
  const nodes: React.ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN.exec(json)) !== null) {
    if (m.index > last)
      nodes.push(<span key={key++} className="text-slate-500">{json.slice(last, m.index)}</span>);
    const t = m[0];
    let cls: string;
    if (t.startsWith('"')) {
      cls = t.endsWith(":") || t.endsWith('":') ? "text-sky-300" : "text-emerald-400";
    } else if (t === "true" || t === "false") {
      cls = "text-amber-400";
    } else if (t === "null") {
      cls = "text-rose-400/80";
    } else if (/^-?\d/.test(t)) {
      cls = "text-orange-300";
    } else {
      cls = "text-slate-500";
    }
    nodes.push(<span key={key++} className={cls}>{t}</span>);
    last = m.index + t.length;
  }
  if (last < json.length) nodes.push(<span key={key++} className="text-slate-500">{json.slice(last)}</span>);
  return nodes;
}

function JsonModal({ rec, title, onClose }: { rec: object; title: string; onClose: () => void }) {
  const json = JSON.stringify(rec, null, 2);
  const close = useCallback(onClose, [onClose]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [close]);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm"
      onClick={close}
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 12 }}
        transition={{ duration: 0.18 }}
        className="relative flex w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-line-strong bg-ink-850 shadow-2xl"
        style={{ maxHeight: "88vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-line px-4 py-2.5">
          <span className="flex items-center gap-1.5 font-mono text-[11px] text-brand-300">
            <span className="opacity-60">{"{ }"}</span>
            <span className="text-slate-400">{title} · raw JSON</span>
          </span>
          <button
            onClick={close}
            className="rounded-md px-2 py-1 text-[11px] text-slate-500 transition hover:bg-wash-strong hover:text-slate-200"
          >
            esc · close
          </button>
        </div>
        <pre className="min-h-0 flex-1 overflow-auto p-4 font-mono text-[12px] leading-relaxed">
          {colorizeJson(json)}
        </pre>
      </motion.div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Extraction card
// ---------------------------------------------------------------------------

function ExtractionCard() {
  const rec = usePipelineStore((s) => s.state.invoice_record) as InvoiceRecord | undefined;
  const model = usePipelineStore((s) => s.state.extraction_model);
  const latency = usePipelineStore((s) => s.state.extraction_latency_ms);
  const inTok = usePipelineStore((s) => s.state.extraction_input_tokens);
  const outTok = usePipelineStore((s) => s.state.extraction_output_tokens);
  const thinkTok = usePipelineStore((s) => s.state.extraction_thinking_tokens);
  const thinkLevel = usePipelineStore((s) => s.state.extraction_thinking_level);
  const costUsd = usePipelineStore((s) => s.state.extraction_cost_usd);
  const cacheReadTokens = usePipelineStore((s) => s.state.extraction_cache_read_tokens);
  const thoughtText = usePipelineStore((s) => s.state.extraction_thought_text);
  const policySnapshot = usePipelineStore((s) => s.state.policy_snapshot);
  const extractionLabel = useWorkingLabel("extraction");
  const [showJson, setShowJson] = useState(false);
  const [showThoughts, setShowThoughts] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  if (!rec) {
    return (
      <Empty>
        <span className="inline-flex items-center gap-2">
          <Spinner className="text-brand-300" />
          {extractionLabel}
        </span>
      </Empty>
    );
  }
  const currency = fv(rec.currency) === "—" ? "" : fv(rec.currency);
  const totalStr = fv(rec.total);
  const totalNum = totalStr === "—" ? NaN : Number(String(totalStr).replace(/[^0-9.-]+/g, ""));
  const fields: Array<[string, FieldValue | undefined]> = [
    ["Vendor", rec.vendor],
    ["Invoice No", rec.invoice_no],
    ["PO Reference", rec.po_ref],
  ];
  return (
    <>
      {fullscreen && <JsonModal rec={rec} title="invoice_record" onClose={() => setFullscreen(false)} />}
    <div className="space-y-3">
      <div className="flex items-center justify-between border-b border-line pb-2">
        <span className="text-[11px] uppercase tracking-wider text-slate-500">{model ? "Extracted by" : ""}</span>
        <div className="flex items-center gap-2">
          {model && (
            <ModelTag
              model={model}
              ms={latency}
              inTok={inTok}
              outTok={outTok}
              thinkTok={thinkTok}
              thinkLevel={thinkLevel}
              costUsd={costUsd}
              cacheReadTokens={cacheReadTokens}
            />
          )}
          {/* Reasoning toggle — only shown when the model returned thought text */}
          {thoughtText && (
            <button
              onClick={() => setShowThoughts((v) => !v)}
              title="Toggle extraction reasoning"
              className={cn(
                "flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-semibold transition-all duration-150",
                showThoughts
                  ? "border-amber-400/60 bg-amber-500/20 text-amber-200 shadow-[0_0_8px_0_rgba(251,191,36,0.2)] ring-1 ring-amber-400/30"
                  : "border-amber-400/35 bg-amber-500/10 text-amber-300/85 hover:border-amber-400/55 hover:text-amber-200",
              )}
            >
              <span>✦</span>
              <span className="text-[11px] font-normal opacity-90">Reasoning</span>
            </button>
          )}
          {/* JSON toggle — pill button, clearly active/inactive */}
          <button
            onClick={() => setShowJson((v) => !v)}
            title="Toggle raw JSON"
            className={cn(
              "flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[11px] font-semibold transition-all duration-150",
              showJson
                ? "border-brand-400/60 bg-brand-500/25 text-brand-200 shadow-[0_0_8px_0_rgba(99,130,255,0.25)] ring-1 ring-brand-400/30"
                : "border-line-strong bg-wash text-slate-400 hover:border-line-bold hover:text-slate-200",
            )}
          >
            <span>{"{}"}</span>
            <span className="text-[11px] font-normal opacity-70">{showJson ? "JSON" : "JSON"}</span>
          </button>
        </div>
      </div>
      {showJson ? (
        <div className="rounded-lg border border-line bg-ink-800 overflow-hidden">
          <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
            <span className="font-mono text-[11px] text-slate-500">invoice_record</span>
            <button
              onClick={() => setFullscreen(true)}
              title="Expand to fullscreen"
              className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-slate-500 transition hover:bg-wash-strong hover:text-slate-300"
            >
              <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
                <path d="M1.5 1h4v1.5h-2.5v2.5h-1.5v-4zm9 0h4v4h-1.5v-2.5h-2.5v-1.5zm-9 9h1.5v2.5h2.5v1.5h-4v-4zm10.5 2.5v-2.5h1.5v4h-4v-1.5h2.5z"/>
              </svg>
              Expand
            </button>
          </div>
          <pre className="max-h-96 overflow-auto p-3 font-mono text-[11px] leading-relaxed">
            {colorizeJson(JSON.stringify(rec, null, 2))}
          </pre>
        </div>
      ) : (
        <>
          <motion.div variants={staggerParent} initial="initial" animate="animate" className="space-y-2">
            {fields.map(([label, f]) => {
              const present = !!(f?.value && String(f.value).trim());
              return (
                <motion.div key={label} variants={staggerItem}>
                  <Row label={label}>
                    {present ? (
                      <>
                        <span className="text-sm text-slate-100">{fv(f)}</span>
                        <GroundingBadge value={f?.source_grounding} />
                      </>
                    ) : (
                      <>
                        <span className="text-sm italic text-slate-500">Not found</span>
                        <span className="rounded-full border border-line-strong bg-wash px-1.5 py-0.5 text-[11px] font-medium text-slate-500">
                          n/a
                        </span>
                      </>
                    )}
                  </Row>
                </motion.div>
              );
            })}
            <motion.div variants={staggerItem}>
              <Row label="Total">
                <span className="text-sm font-semibold text-gradient">
                  {totalStr === "—" || Number.isNaN(totalNum) ? (
                    totalStr
                  ) : (
                    <CountUp value={totalNum} decimals={2} group prefix={currency ? `${currency} ` : ""} />
                  )}
                </span>
                <GroundingBadge value={rec.total?.source_grounding} />
              </Row>
            </motion.div>
          </motion.div>

          {rec.line_items && rec.line_items.length > 0 && (
            <div className="border-t border-line pt-3">
              <p className="mb-1.5 text-[11px] uppercase tracking-wider text-slate-400">
                Line items ({rec.line_items.length})
              </p>
              {rec.line_items_complete === false && (
                <div className="mb-2 flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-200">
                  <WarnDot />
                  <span>
                    Line items incomplete — extraction was truncated ({rec.line_items.length} captured).
                    The list below is partial and the total can't be reconciled reliably.
                  </span>
                </div>
              )}
              <div className="max-h-80 space-y-1 overflow-y-auto">
                {rec.line_items.map((li, i) => {
                  const attrs = (li.attributes ?? []).filter((a) => a.label && a.value);
                  return (
                    <div key={i} className="rounded-md px-1.5 py-1 text-xs text-slate-300 transition hover:bg-wash">
                      <div className="flex items-center justify-between gap-3">
                        <span className="truncate">{li.name || "—"}</span>
                        <span className="flex items-center gap-2">
                          <span className="font-mono text-slate-400">{li.amount || "—"}</span>
                          <GroundingBadge value={li.source_grounding} />
                        </span>
                      </div>
                      {attrs.length > 0 && (
                        <div className="mt-0.5 truncate text-[11px] text-slate-500">
                          {attrs.map((a) => `${a.label}: ${a.value}`).join(" · ")}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </>
      )}

      <div className="flex items-center justify-between border-t border-line pt-2 text-[11px] text-slate-400">
        <span>Fields grounded in the document</span>
        <span className="font-mono text-slate-300">
          {(() => {
            const { grounded, total } = requiredFieldsGrounded(rec, policySnapshot);
            return `${grounded}/${total}`;
          })()}
        </span>
      </div>

      {showThoughts && thoughtText && (
        <div className="rounded-xl border border-amber-400/15 bg-amber-500/[0.04] p-3">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-amber-300/70">
            Extraction reasoning
          </p>
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-300/80">
            {thoughtText}
          </pre>
        </div>
      )}

      <AgentCostProjection
        model={model}
        inTok={inTok}
        outTok={outTok}
        thinkTok={thinkTok}
        thinkLevel={thinkLevel}
        cacheReadTokens={cacheReadTokens}
      />
    </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Screening card (screening agent assessment)
// ---------------------------------------------------------------------------

const strengthColor = (s: SignalStrength) =>
  s === "strong" ? "text-live-400" : s === "weak" ? "text-amber-300" : "text-slate-500";

function ScreeningCard() {
  const assessment = usePipelineStore((s) => s.state.screening_assessment) as
    | InvoiceScreeningAssessment
    | undefined;
  const model = usePipelineStore((s) => s.state.screening_model);
  const latency = usePipelineStore((s) => s.state.screening_latency_ms);
  const inTok = usePipelineStore((s) => s.state.screening_input_tokens);
  const outTok = usePipelineStore((s) => s.state.screening_output_tokens);
  const thinkTok = usePipelineStore((s) => s.state.screening_thinking_tokens);
  const cacheReadTokens = usePipelineStore((s) => s.state.screening_cache_read_tokens);
  const [showJson, setShowJson] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  if (!assessment) return <Empty>Waiting for screening result…</Empty>;

  const attachment = assessment.attachment_signal ?? "absent";
  const body = assessment.body_signal ?? "absent";
  const subject = assessment.subject_signal ?? "absent";
  const signals = assessment.observed_signals ?? [];
  const rationale = assessment.assessment_rationale ?? "";

  return (
    <>
      {fullscreen && (
        <JsonModal rec={assessment} title="screening_assessment" onClose={() => setFullscreen(false)} />
      )}
      <div className="space-y-3">
        <div className="flex items-center justify-between border-b border-line pb-2">
          <span className="text-[11px] uppercase tracking-wider text-slate-500">{model ? "Screened by" : ""}</span>
          <div className="flex items-center gap-2">
            {model && <ModelTag model={model} ms={latency} inTok={inTok} outTok={outTok} thinkTok={thinkTok} cacheReadTokens={cacheReadTokens} />}
            <button
              onClick={() => setShowJson((v) => !v)}
              title="Toggle raw JSON"
              className={cn(
                "flex items-center gap-1 rounded-md border px-2 py-1 font-mono text-[11px] font-semibold transition-all duration-150",
                showJson
                  ? "border-brand-400/60 bg-brand-500/25 text-brand-200 shadow-[0_0_8px_0_rgba(99,130,255,0.25)] ring-1 ring-brand-400/30"
                  : "border-line-strong bg-wash text-slate-400 hover:border-line-bold hover:text-slate-200",
              )}
            >
              <span>{"{}"}</span>
              <span className="text-[11px] font-normal opacity-70">JSON</span>
            </button>
          </div>
        </div>

        {showJson ? (
          <div className="rounded-lg border border-line bg-ink-800 overflow-hidden">
            <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
              <span className="font-mono text-[11px] text-slate-500">screening_assessment</span>
              <button
                onClick={() => setFullscreen(true)}
                title="Expand to fullscreen"
                className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-slate-500 transition hover:bg-wash-strong hover:text-slate-300"
              >
                <svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M1.5 1h4v1.5h-2.5v2.5h-1.5v-4zm9 0h4v4h-1.5v-2.5h-2.5v-1.5zm-9 9h1.5v2.5h2.5v1.5h-4v-4zm10.5 2.5v-2.5h1.5v4h-4v-1.5h2.5z"/>
                </svg>
                Expand
              </button>
            </div>
            <pre className="max-h-96 overflow-auto p-3 font-mono text-[11px] leading-relaxed">
              {colorizeJson(JSON.stringify(assessment, null, 2))}
            </pre>
          </div>
        ) : (
          <motion.div variants={staggerParent} initial="initial" animate="animate" className="space-y-2">
            <motion.div variants={staggerItem} className="space-y-1">
              <p className="mb-1.5 text-[11px] uppercase tracking-wider text-slate-400">Signal Evidence</p>
              {([
                ["Attachment", attachment],
                ["Body", body],
                ["Subject", subject],
              ] as const).map(([label, strength]) => (
                <Row key={label} label={label}>
                  <span className={cn("text-xs font-semibold uppercase tracking-wide", strengthColor(strength))}>
                    {strength}
                  </span>
                </Row>
              ))}
            </motion.div>

            {signals.length > 0 && (
              <motion.div variants={staggerItem} className="border-t border-line pt-2">
                <p className="mb-1.5 text-[11px] uppercase tracking-wider text-slate-400">Detected Signals</p>
                <div className="space-y-1">
                  {signals.map((sig, i) => (
                    <div key={i} className="flex items-start gap-1.5 rounded-md px-1.5 py-0.5 text-xs text-slate-300 transition hover:bg-wash">
                      <span className="mt-0.5 text-teal-400">•</span>
                      <span>{sig}</span>
                    </div>
                  ))}
                </div>
              </motion.div>
            )}

            {rationale && (
              <motion.div variants={staggerItem} className="border-t border-line pt-2">
                <p className="mb-1 text-[11px] uppercase tracking-wider text-slate-400">Rationale</p>
                <p className="text-xs leading-relaxed text-slate-300">{rationale}</p>
              </motion.div>
            )}
          </motion.div>
        )}

        <AgentCostProjection model={model} inTok={inTok} outTok={outTok} thinkTok={thinkTok} cacheReadTokens={cacheReadTokens} />
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Agent Memory card (advisory approve/reject precedent, read-only)
// ---------------------------------------------------------------------------

function MemoryCard() {
  const precedent = usePipelineStore((s) => s.state.decision_precedents);

  if (!precedent || !precedent.total_count) {
    return <Empty>No prior decisions on file for this vendor + flag yet.</Empty>;
  }

  const { vendor, approve_count, reject_count, recommendation, narrative } = precedent;

  return (
    <div className="space-y-3">
      {/* Lead with the plain-English insight — deterministic (Python template,
          no LLM), but written as a sentence rather than a stat line so it
          reads as an insight, not a debug dump. */}
      <motion.div
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        className={cn(
          "rounded-xl border p-3.5 text-sm leading-relaxed",
          recommendation
            ? "border-violet-400/30 bg-violet-500/[0.08] text-violet-100"
            : "border-line-strong bg-wash text-slate-300",
        )}
      >
        {narrative}
      </motion.div>

      {/* Compact supporting figures — the data backing the sentence above. */}
      <div className="flex items-center justify-between border-t border-line pt-2 text-[11px] text-slate-400">
        <span className="truncate">{vendor}</span>
        <span className="flex shrink-0 items-center gap-1.5">
          <span className="text-live-300">{approve_count} approved</span>
          <span className="text-slate-600">·</span>
          <span className="text-rose-300">{reject_count} rejected</span>
        </span>
      </div>
    </div>
  );
}

function WarnDot() {
  return (
    <span className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full bg-amber-400/15 text-amber-300">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
        <path d="M12 8v5M12 17h.01" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

function UserIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="8" r="3.2" />
      <path d="M5 20a7 7 0 0 1 14 0" strokeLinecap="round" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Editable rejection email (Send / Revise)
// ---------------------------------------------------------------------------

function EmailEditor() {
  const draft = usePipelineStore((s) => s.state.rejection_email_draft);
  const recipient = usePipelineStore((s) => s.state.invoice_sender_email);
  const interrupt = usePipelineStore((s) => s.currentInterrupt);
  const submitResume = usePipelineStore((s) => s.submitResume);
  const status = usePipelineStore((s) => s.status);
  const model = usePipelineStore((s) => s.state.draft_email_model);
  const latency = usePipelineStore((s) => s.state.draft_email_latency_ms);
  const inTok = usePipelineStore((s) => s.state.draft_email_input_tokens);
  const outTok = usePipelineStore((s) => s.state.draft_email_output_tokens);
  const thinkTok = usePipelineStore((s) => s.state.draft_email_thinking_tokens);
  const cacheReadTokens = usePipelineStore((s) => s.state.draft_email_cache_read_tokens);

  const [subject, setSubject] = useState(draft?.subject ?? "");
  const [body, setBody] = useState(draft?.body ?? "");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  // Read-only during a replay: the recorded decision is played from the replay bar.
  const replaying = useReplay((s) => s.active);
  const awaiting = status === "awaiting_input" && interrupt?.kind === "email_review" && !replaying;

  const send = () => {
    if (!interrupt) return;
    setBusy("send");
    void submitResume(interrupt.interruptId, { decision: "send", subject, body });
  };
  const revise = () => {
    if (!interrupt) return;
    setBusy("revise");
    void submitResume(interrupt.interruptId, { feedback: feedback || "Please revise the email." });
  };

  return (
    <div className="space-y-3">
      {model && (
        <div className="flex items-center justify-between border-b border-line pb-2">
          <span className="text-[11px] uppercase tracking-wider text-slate-500">Drafted by</span>
          <ModelTag model={model} ms={latency} inTok={inTok} outTok={outTok} thinkTok={thinkTok} cacheReadTokens={cacheReadTokens} />
        </div>
      )}
      <Kv label="To" value={recipient ?? "vendor"} />
      <label className="block">
        <span className="text-[11px] uppercase tracking-wider text-slate-400">Subject</span>
        <input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          disabled={!awaiting}
          className="glass mt-1 w-full rounded-lg px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-brand-400/60 focus:ring-1 focus:ring-brand-400/30 disabled:opacity-60"
        />
      </label>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wider text-slate-400">Body</span>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={!awaiting}
          rows={7}
          className="glass mt-1 w-full resize-y rounded-lg px-3 py-2 text-sm leading-relaxed text-slate-100 outline-none transition focus:border-brand-400/60 focus:ring-1 focus:ring-brand-400/30 disabled:opacity-60"
        />
      </label>
      <label className="block">
        <span className="text-[11px] uppercase tracking-wider text-slate-400">Revision instructions (optional)</span>
        <input
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          disabled={!awaiting}
          placeholder="e.g. soften the tone, add a contact line…"
          className="glass mt-1 w-full rounded-lg px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-accent-400/60 focus:ring-1 focus:ring-accent-400/30 disabled:opacity-60"
        />
      </label>
      {busy === "send" ? (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center justify-center gap-2 rounded-xl bg-live-500/12 px-4 py-2.5 text-sm font-semibold text-live-300 ring-1 ring-live-500/30"
        >
          {status === "streaming" ? <Spinner /> : <CheckMini />}
          {status === "streaming" ? "Sending to vendor" : "Sent to vendor"}
        </motion.div>
      ) : (
        <div className="flex gap-2 pt-1">
          <motion.button
            whileHover={awaiting ? { scale: 1.02 } : undefined}
            whileTap={awaiting ? { scale: 0.98 } : undefined}
            onClick={send}
            disabled={!awaiting}
            className="flex-1 rounded-xl bg-gradient-to-r from-brand-400 to-brand-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-accent-500/25 transition hover:brightness-110 disabled:opacity-50"
          >
            Send to vendor
          </motion.button>
          <motion.button
            whileHover={awaiting ? { scale: 1.02 } : undefined}
            whileTap={awaiting ? { scale: 0.98 } : undefined}
            onClick={revise}
            disabled={!awaiting}
            className="glass flex inline-flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-slate-200 transition hover:brightness-125 disabled:opacity-50"
          >
            {busy === "revise" && <Spinner className="h-3.5 w-3.5" />}
            {busy === "revise" ? "Revising with AI" : "Revise with AI"}
          </motion.button>
        </div>
      )}
      <AgentCostProjection model={model} inTok={inTok} outTok={outTok} thinkTok={thinkTok} cacheReadTokens={cacheReadTokens} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small layout helpers
// ---------------------------------------------------------------------------

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-slate-500">{label}</span>
      <div className="flex items-center gap-2">{children}</div>
    </div>
  );
}

function Kv({ label, value, mono, strong }: { label: string; value: string; mono?: boolean; strong?: boolean }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wider text-slate-500">{label}</p>
      <p className={cn("text-sm text-slate-100", mono && "font-mono text-xs text-brand-300", strong && "font-semibold")}>
        {value}
      </p>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-4 text-sm text-slate-500">{children}</div>;
}

// In dev, force a full page reload when this module changes. A partial Fast
// Refresh swap of these surface components can desync from the singleton store /
// A2UI engine (see usePipelineStore.ts) and leave a long-lived tab rendering a
// stale surface tree (e.g. an approval gate with no buttons). A full reload
// guarantees the cards always re-render from fresh state.
if (import.meta.hot) import.meta.hot.accept(() => window.location.reload());
