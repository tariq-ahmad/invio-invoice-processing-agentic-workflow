import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { useRunStats, type RangeDays } from "../../store/useRunStats";
import { usePipelineStore } from "../../store/usePipelineStore";
import { outcomeBreakdown, summarizeStats } from "../../lib/statsSummary";
import { formatLatency } from "../../lib/duration";
import { cn } from "../../lib/utils";

const REFRESH_MS = 15_000;

const RANGES: { id: RangeDays; label: string }[] = [
  { id: 1, label: "24 h" },
  { id: 7, label: "7 days" },
  { id: 30, label: "30 days" },
  { id: 0, label: "All" },
];

const money = (n: number) =>
  n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: n >= 1000 ? 0 : 2 });
const pct = (n: number) => `${Math.round(n * 100)}%`;

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-line bg-wash px-3 py-2">
      <p className="truncate text-[10px] font-semibold uppercase tracking-wider text-slate-500">{label}</p>
      <p className="mt-0.5 truncate text-lg font-semibold tabular-nums leading-tight tracking-tight text-slate-50">{value}</p>
      {hint && <p className="truncate text-[10px] leading-snug text-slate-500">{hint}</p>}
    </div>
  );
}

/** Outcome colours run green -> light green -> muted red: more automation reads as better,
 * a rejection reads as "stopped" without alarm. */
const OUTCOME_TONES = {
  auto: { bar: "bg-emerald-400", card: "border-emerald-400/20 bg-emerald-500/5", text: "text-emerald-400" },
  review: { bar: "bg-emerald-400/60", card: "border-emerald-400/20 bg-emerald-500/5", text: "text-emerald-400" },
  rejected: { bar: "bg-rose-400/70", card: "border-rose-400/20 bg-rose-500/5", text: "text-rose-400" },
} as const;

/** One outcome: label, a one-line note, count and share of all invoices. */
function OutcomeRow({
  label,
  sublabel,
  count,
  total,
  tone,
}: {
  label: string;
  sublabel: string;
  count: number;
  total: number;
  tone: keyof typeof OUTCOME_TONES;
}) {
  const share = total > 0 ? Math.round((count / total) * 100) : 0;
  const t = OUTCOME_TONES[tone];
  return (
    <div className={cn("flex items-center gap-3 rounded-lg border px-3 py-2", t.card)}>
      <span className={cn("h-7 w-1 shrink-0 rounded-full", t.bar)} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className={cn("truncate text-[10px] font-semibold uppercase tracking-wider", t.text)}>{label}</p>
        <p className="truncate text-[10px] leading-tight text-slate-500">{sublabel}</p>
      </div>
      <div className="text-right">
        <p className="text-base font-bold tabular-nums leading-tight text-slate-50">{count.toLocaleString()}</p>
        {total > 0 && <p className={cn("text-[10px] font-semibold tabular-nums leading-tight", t.text)}>{share}%</p>}
      </div>
    </div>
  );
}

/** One stacked bar over every invoice, coloured per outcome. */
function OutcomeBar({
  segments,
  total,
}: {
  segments: { label: string; count: number; shade: string }[];
  total: number;
}) {
  const summary = segments.filter((x) => x.count > 0).map((x) => `${x.count} ${x.label}`).join(", ");
  return (
    <div
      role="img"
      aria-label={summary || "No outcomes"}
      className="flex h-1.5 w-full gap-0.5 overflow-hidden rounded-full bg-wash-strong"
    >
      {segments.map(
        (x) =>
          x.count > 0 && (
            <div key={x.label} className={cn("h-full rounded-full", x.shade)} style={{ width: `${(x.count / total) * 100}%` }} />
          ),
      )}
    </div>
  );
}

function Loading() {
  return (
    <div className="space-y-3" role="status" aria-label="Loading stats">
      <div className="grid grid-cols-2 gap-2">
        {[0, 1, 2, 3].map((i) => (
          <span key={i} className="skel block h-[62px] rounded-xl" />
        ))}
      </div>
      <span className="skel block h-24 rounded-xl" />
    </div>
  );
}

/** Slide-over with pipeline insights read live from Firestore (via GET /stats): every
 * invoice the agent has completed, shared across users and sessions. Refreshes while
 * open and after each run finishes. Replays aren't recorded, so never count.
 * A narrow vertical panel kept short enough to fit one screen: KPI tiles, total
 * value, then three outcomes in a single colour. */
export function StatsDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { data, load, error, fetchedAt, range } = useRunStats();
  const setRange = useRunStats((s) => s.setRange);
  const refresh = useRunStats((s) => s.refresh);
  const closeRef = useRef<HTMLButtonElement>(null);
  const s = data ? summarizeStats(data) : null;

  // Live: fetch on open, poll while open, and refetch right after a run completes.
  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    void refresh();
    const poll = setInterval(() => void refresh(), REFRESH_MS);
    let later: ReturnType<typeof setTimeout> | null = null;
    const unsub = usePipelineStore.subscribe((st, prev) => {
      if (st.status === "complete" && prev.status !== "complete") {
        later = setTimeout(() => void refresh(), 1500); // let the backend finish writing
      }
    });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      clearInterval(poll);
      if (later) clearTimeout(later);
      unsub();
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, refresh]);

  const o = s && data ? outcomeBreakdown(data.by_status, s.autoPosted) : null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <div className="no-print fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="Insights">
          <motion.div
            className="absolute inset-0 bg-black/40"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
          />
          <motion.aside
            className="glass-elevated absolute inset-y-0 right-0 flex w-full max-w-sm flex-col rounded-l-2xl"
            initial={{ x: 40, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 40, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
          >
            <header className="border-b border-line px-5 py-3">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-base font-semibold text-slate-50">Insights</h2>
                <button
                  ref={closeRef}
                  type="button"
                  onClick={onClose}
                  aria-label="Close insights"
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-slate-400 transition hover:bg-wash-strong hover:text-slate-100"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden>
                    <path d="M6 6l12 12M18 6L6 18" />
                  </svg>
                </button>
              </div>
              <div role="radiogroup" aria-label="Time range" className="mt-2.5 flex rounded-xl bg-wash p-0.5">
                {RANGES.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    role="radio"
                    aria-checked={range === r.id}
                    onClick={() => setRange(r.id)}
                    className={cn(
                      "flex-1 rounded-[10px] px-2 py-1 text-xs font-medium transition-colors",
                      range === r.id ? "bg-brand-500/20 text-brand-200" : "text-slate-400 hover:text-slate-100",
                    )}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </header>

            <div className="scroll-slim flex-1 space-y-3.5 overflow-y-auto px-5 py-3.5">
              {error && (
                <div role="alert" className="rounded-lg border border-rose-400/30 bg-rose-500/[0.08] px-3 py-2 text-xs text-rose-200">
                  Couldn't refresh: {error}
                  {data ? " Showing the last numbers we got." : ""}
                </div>
              )}

              {load === "loading" && !data ? (
                <Loading />
              ) : !s ? null : s.invoices === 0 ? (
                <div className="rounded-xl border border-dashed border-line-strong px-4 py-10 text-center">
                  <p className="text-sm font-medium text-slate-200">No runs recorded {range ? "in this range" : "yet"}</p>
                  <p className="mt-1 text-xs text-slate-500">Each invoice is recorded when its run finishes.</p>
                </div>
              ) : (
                <>
                  {/* KPI tiles */}
                  <div className="grid grid-cols-2 gap-2">
                    <Tile label="Invoices" value={s.invoices.toLocaleString()} hint="processed" />
                    <Tile label="Auto-posted" value={s.autoRate != null ? pct(s.autoRate) : "—"} hint="no human step" />
                    <Tile
                      label="Avg model time"
                      value={s.avgDecisionMs != null ? formatLatency(s.avgDecisionMs) : "—"}
                      hint="extraction + drafting"
                    />
                    <Tile
                      label="Avg cost"
                      value={s.avgCostUsd != null ? `$${s.avgCostUsd.toFixed(s.avgCostUsd < 0.1 ? 4 : 2)}` : "—"}
                      hint="per invoice"
                    />
                  </div>

                  {/* Total value */}
                  <section className="flex items-center justify-between gap-3 rounded-xl border border-line bg-wash px-3 py-2">
                    <div>
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Total value processed</p>
                      <p className="text-[10px] text-slate-500">USD, policy FX rates</p>
                    </div>
                    <p className="text-lg font-semibold tabular-nums text-slate-50">{money(s.amountTotal)}</p>
                  </section>

                  {/* Outcomes: three buckets, green -> light green -> red */}
                  {o && (
                    <section className="space-y-2">
                      <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">Outcomes</h3>
                      <OutcomeBar
                        total={s.invoices}
                        segments={[
                          { label: "straight-through", count: o.autoPosted, shade: OUTCOME_TONES.auto.bar },
                          { label: "posted after human review", count: o.postedAfterReview, shade: OUTCOME_TONES.review.bar },
                          { label: "rejected", count: o.rejectedTotal, shade: OUTCOME_TONES.rejected.bar },
                        ]}
                      />
                      <OutcomeRow
                        label="Straight-Through"
                        sublabel="Posted with no human involvement"
                        count={o.autoPosted}
                        total={s.invoices}
                        tone="auto"
                      />
                      <OutcomeRow
                        label="Posted After Human Review"
                        sublabel="Human-approved"
                        count={o.postedAfterReview}
                        total={s.invoices}
                        tone="review"
                      />
                      <OutcomeRow
                        label="Rejected"
                        sublabel={`Vendor declined ${o.vendorDeclined} · Duplicate ${o.duplicates} · Security ${o.securityFlagged}`}
                        count={o.rejectedTotal}
                        total={s.invoices}
                        tone="rejected"
                      />
                    </section>
                  )}

                  {data?.truncated && (
                    <p className="text-[11px] text-slate-500">Showing the most recent runs only; older ones aren't included.</p>
                  )}
                </>
              )}
            </div>

            <footer className="flex items-center justify-between gap-3 border-t border-line px-5 py-2.5">
              <div className="flex flex-col gap-0.5">
                <p className="flex items-center gap-2 text-xs text-slate-400" aria-live="polite">
                  <span
                    className={cn(
                      "h-2 w-2 shrink-0 rounded-full",
                      load === "error" ? "bg-rose-400" : load === "loading" ? "bg-amber-400" : "bg-done-400",
                    )}
                    aria-hidden
                  />
                  {fetchedAt ? `Last updated at ${new Date(fetchedAt).toLocaleTimeString()}` : "Loading…"}
                </p>
                <p className="pl-4 text-[10px] text-slate-500">Auto-refreshes every 15s</p>
              </div>
              <button
                type="button"
                onClick={() => void refresh()}
                className={cn(
                  "rounded-lg border px-2.5 py-1 text-xs font-medium transition-all",
                  "border-brand-400/40 bg-brand-500/10 text-brand-300",
                  "ring-1 ring-brand-400/20 ring-offset-0",
                  "hover:border-brand-400/70 hover:bg-brand-500/20 hover:text-brand-200 hover:ring-brand-400/40",
                  "active:scale-95",
                )}
              >
                Refresh
              </button>
            </footer>
          </motion.aside>
        </div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
