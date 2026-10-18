import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore } from "../../store/usePipelineStore";
import { deriveRunCost, type RunModelCost } from "../../lib/runCost";
import { CountUp } from "../common/CountUp";
import { AgentGlyph } from "../common/AgentGlyph";
import { staggerItem, staggerParent, springSoft, prefersReducedMotion } from "../../lib/motion";

/** Brightest → dimmest indigo shade per model tier, so the chip stack reads as
 * a capability gradient (Pro brightest / priciest → Flash-Lite dimmest /
 * cheapest) without introducing a second hue into the single-accent palette. */
const TIER_DOT: Record<string, string> = {
  "gemini-3.1-pro-preview": "bg-brand-300",
  "gemini-3.8-flash": "bg-brand-500",
  "gemini-3.5-flash-lite": "bg-brand-600",
};

/** Wraps a number so it visually "pops" (brief scale + brightness flash) every
 * time it increases — remounting on `bumpKey` replays the enter transition. */
function BumpNumber({
  value,
  bumpKey,
  ...countUpProps
}: {
  value: number;
  bumpKey: number;
} & Omit<React.ComponentProps<typeof CountUp>, "value">) {
  const reduced = prefersReducedMotion();
  return (
    <motion.span
      key={bumpKey}
      initial={reduced ? false : { scale: 1.3, filter: "brightness(1.7)" }}
      animate={{ scale: 1, filter: "brightness(1)" }}
      transition={springSoft}
      style={{ display: "inline-block" }}
    >
      <CountUp value={value} {...countUpProps} />
    </motion.span>
  );
}

/** Tracks how many times `value` has increased, for keying the bump/delta
 * animations, plus the size of the most recent jump. */
function useIncrement(value: number) {
  const prev = useRef(value);
  const [tick, setTick] = useState(0);
  const [delta, setDelta] = useState(0);
  useEffect(() => {
    if (value > prev.current + 1e-9) {
      setDelta(value - prev.current);
      setTick((t) => t + 1);
    }
    prev.current = value;
  }, [value]);
  return { tick, delta };
}

function ModelChip({ model }: { model: RunModelCost }) {
  const cost = useIncrement(model.cost);
  return (
    <motion.div
      layout
      variants={staggerItem}
      initial="initial"
      animate="animate"
      exit={{ opacity: 0, scale: 0.6 }}
      className="flex items-center gap-1.5 rounded-full border border-brand-400/25 bg-wash px-2 py-0.5"
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${TIER_DOT[model.model] ?? "bg-brand-400"}`} />
      <span className="text-[11px] font-medium text-slate-300">{model.label}</span>
      <span className="text-[11px] font-semibold text-brand-200 tabular-nums">
        <BumpNumber value={model.cost} bumpKey={cost.tick} decimals={4} prefix="$" />
      </span>
    </motion.div>
  );
}

/** Live token/cost readout for the invoice currently being processed. Pulls
 * straight from the per-step `*_input_tokens`/`*_output_tokens`/`*_model`
 * fields ADK streams into pipeline state, so it climbs in step with the rail
 * and zeroes on `reset()` without any extra plumbing. */
export function SessionCostMeter() {
  const state = usePipelineStore((s) => s.state);
  const status = usePipelineStore((s) => s.status);
  const [open, setOpen] = useState(false);
  const { inTokens, outTokens, totalCost, cacheReadTokens, cacheSavingsUsd, models } = deriveRunCost(state);
  const hasData = models.length > 0;
  const streaming = status === "streaming";

  const inBump = useIncrement(inTokens);
  const outBump = useIncrement(outTokens);
  const costBump = useIncrement(totalCost);

  const [deltaVisible, setDeltaVisible] = useState(false);
  useEffect(() => {
    if (costBump.tick === 0) return;
    setDeltaVisible(true);
    const timer = setTimeout(() => setDeltaVisible(false), 1200);
    return () => clearTimeout(timer);
  }, [costBump.tick]);

  return (
    <div
      className="relative"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`relative flex items-center gap-2 rounded-full border px-3 py-1.5 transition-colors duration-300 ${
          hasData ? "gradient-border border-brand-400/30 bg-gradient-to-r from-brand-500/10 to-brand-600/10" : "border-line-strong bg-wash"
        }`}
      >
        <span className="relative flex h-4 w-4 shrink-0 items-center justify-center">
          <AgentGlyph size={14} animate={streaming} />
        </span>

        <span className="text-[11px] uppercase tracking-wider text-slate-500">Session</span>

        <span className="text-xs font-bold tabular-nums">
          <BumpNumber
            value={totalCost}
            bumpKey={costBump.tick}
            decimals={4}
            prefix="$"
            className="text-gradient"
          />
        </span>

        <div className="hidden items-center gap-2 border-l border-line-strong pl-2 text-[11px] text-slate-400 sm:flex">
          <span className="flex items-center gap-1">
            <span aria-hidden className="text-brand-300">↓</span>
            <BumpNumber value={inTokens} bumpKey={inBump.tick} group className="tabular-nums text-slate-200" />
          </span>
          <span className="flex items-center gap-1">
            <span aria-hidden className="text-brand-300">↑</span>
            <BumpNumber value={outTokens} bumpKey={outBump.tick} group className="tabular-nums text-slate-200" />
          </span>
        </div>

        <motion.div className="hidden items-center gap-1.5 md:flex" variants={staggerParent} initial="initial" animate="animate">
          {models.map((m) => (
            <ModelChip key={m.model} model={m} />
          ))}
        </motion.div>
      </button>

      {/* Floating "+Δ" pulse when total cost jumps — replayed via the tick key,
          auto-dismissed a moment later so it doesn't linger after the run ends. */}
      <AnimatePresence>
        {deltaVisible && (
          <motion.span
            key={costBump.tick}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: -6 }}
            exit={{ opacity: 0, y: -18 }}
            transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1] }}
            className="pointer-events-none absolute -top-1 right-3 text-[11px] font-semibold text-brand-300"
          >
            +${costBump.delta.toFixed(4)}
          </motion.span>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {open && hasData && (
          <motion.div
            initial="initial"
            animate="animate"
            exit="initial"
            variants={staggerParent}
            className="absolute right-0 top-full z-30 mt-2 w-64 rounded-xl border border-line-strong bg-ink-850 p-3 shadow-[var(--shadow-raised)]"
          >
            <div className="mb-2 text-[11px] uppercase tracking-wider text-slate-500">
              Cost by model — this run
            </div>
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-slate-500">
                  <th className="pb-1.5 text-left font-normal">Model</th>
                  <th className="pb-1.5 text-right font-normal">In</th>
                  <th className="pb-1.5 text-right font-normal">Out</th>
                  <th className="pb-1.5 text-right font-normal">Cost</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <motion.tr key={m.model} variants={staggerItem} className="border-t border-line">
                    <td className="py-1.5 pr-2">
                      <span className="flex items-center gap-1.5 text-slate-300">
                        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${TIER_DOT[m.model] ?? "bg-brand-400"}`} />
                        {m.label}
                      </span>
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-slate-400">
                      {m.inTokens.toLocaleString()}
                    </td>
                    <td className="py-1.5 text-right tabular-nums text-slate-400">
                      {m.outTokens.toLocaleString()}
                    </td>
                    <td className="py-1.5 text-right tabular-nums font-semibold text-brand-200">
                      ${m.cost.toFixed(4)}
                    </td>
                  </motion.tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-line-bold">
                  <td colSpan={3} className="pt-2 text-slate-400">Total</td>
                  <td className="pt-2 text-right tabular-nums font-semibold text-gradient">
                    ${totalCost.toFixed(4)}
                  </td>
                </tr>
                {cacheSavingsUsd > 0.000001 && (
                  <tr>
                    <td
                      colSpan={3}
                      className="pt-1 text-done-400 text-[11px] cursor-help"
                      title="Input tokens served from Gemini implicit context cache, billed at 10% of the standard input rate. Cache applies only to input tokens — thinking and output tokens are not cached."
                    >
                      Cache savings · {cacheReadTokens.toLocaleString()} tkns
                    </td>
                    <td
                      className="pt-1 text-right tabular-nums font-semibold text-done-300 text-[11px]"
                      title="90% discount on tokens served from Gemini implicit context cache"
                    >
                      -${cacheSavingsUsd.toFixed(4)}
                    </td>
                  </tr>
                )}
              </tfoot>
            </table>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
