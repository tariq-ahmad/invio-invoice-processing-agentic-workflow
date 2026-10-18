/**
 * Conversation card for the Security Scan (Model Armor). It appears the moment the
 * scan starts and shows it running, then resolves in place. Two sections, in the
 * order a reviewer reads them:
 *  - Checks  the filters applied, and for a flagged one why and where
 *  - Verdict passed / quarantined / held / flagged / not scanned, and what happened
 *            to the email. It is the final word: nothing follows it for a caught email.
 * Driven entirely by the stage status and the verdict in pipeline state, so it never
 * claims an outcome the backend didn't report.
 */
import { useEffect, useId, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore } from "../../store/usePipelineStore";
import { STAGES, type ArmorStageId } from "../../store/stageMap";
import type { ArmorVerdict } from "../../api/types";
import {
  ARMOR_COPY,
  OUTCOME_WORD,
  armorCaughtMessage,
  armorFilterRows,
  armorOutcome,
  describeFinding,
  armorScanningFilters,
  armorVerdict,
  filterLabel,
  outcomeTone,
  type ArmorFilterRow,
  type ArmorOutcome,
  type ArmorTone,
} from "../../lib/armor";
import { cardReveal, staggerItem, staggerParent } from "../../lib/motion";
import { formatLatency } from "../../lib/duration";
import { cn } from "../../lib/utils";

type Phase = "scanning" | ArmorTone;

const TONE = {
  scanning: {
    ring: "ring-brand-400/25",
    badge: "border-brand-400/40 bg-brand-500/15 text-brand-200",
    head: "text-brand-200",
    section: "border-brand-400/25 bg-brand-500/[0.05]",
    stroke: "#a78bfa",
    fill: "rgba(124,92,245,0.18)",
  },
  pass: {
    ring: "ring-done-500/25",
    badge: "border-done-500/40 bg-done-500/15 text-done-300",
    head: "text-done-300",
    section: "border-done-500/25 bg-done-500/[0.05]",
    stroke: "#34d399",
    fill: "rgba(16,185,129,0.20)",
  },
  stop: {
    ring: "ring-rose-400/45 shadow-[0_10px_40px_-14px_rgba(244,63,94,0.55)]",
    badge: "border-rose-400/45 bg-rose-500/15 text-rose-200",
    head: "text-rose-200",
    section: "border-rose-400/35 bg-rose-500/[0.07]",
    stroke: "#fb7185",
    fill: "rgba(244,63,94,0.20)",
  },
  warn: {
    ring: "ring-amber-400/30",
    badge: "border-amber-400/40 bg-amber-400/15 text-amber-200",
    head: "text-amber-200",
    section: "border-amber-400/30 bg-amber-400/[0.06]",
    stroke: "#fbbf24",
    fill: "rgba(245,158,11,0.20)",
  },
} as const;

export function ArmorCard({ stage }: { stage: ArmorStageId }) {
  const state = usePipelineStore((s) => s.state);
  const status = usePipelineStore((s) => s.stageStatus[stage]);
  const stepNum = usePipelineStore((s) => s.stepNumbers[stage]);
  const activatedAt = usePipelineStore((s) => s.stageActivatedAt[stage]);

  const def = STAGES[stage];
  const copy = ARMOR_COPY[stage];
  const verdict = armorVerdict(stage, state);
  const scanning = status !== "done";
  // A finished scan with no verdict means the backend reported none: say so
  // rather than implying it passed.
  const outcome: ArmorOutcome | undefined = scanning ? undefined : (armorOutcome(verdict) ?? "unscanned");
  const phase: Phase = outcome ? outcomeTone(outcome) : "scanning";
  const t = TONE[phase];

  const rows: ArmorFilterRow[] =
    !scanning && verdict
      ? armorFilterRows(verdict)
      : verdict?.checked?.length
        ? armorFilterRows(verdict)
        : armorScanningFilters(stage).map((f) => ({ ...f, findings: [], result: "clear" as const }));

  // Subtitles carry the live status, so a collapsed section still says how it went.
  const count = (r: ArmorFilterRow["result"]) => rows.filter((row) => row.result === r).length;
  const checksSubtitle = scanning
    ? `${rows.length} filters`
    : [
        `${count("clear")} clear`,
        count("detected") ? `${count("detected")} detected` : "",
        count("allowed") ? `${count("allowed")} not blocked` : "",
      ]
        .filter(Boolean)
        .join(" · ");
  const verdictSubtitle = outcome ? copy.short[outcome] : "Waiting for the scan to finish";

  const detected = verdict?.filters.map(filterLabel).join(", ") ?? "";
  const headline = !outcome
    ? "Scanning"
    : outcome === "pass"
      ? "Passed · no threats found"
      : outcome === "caught"
        ? `Email quarantined · ${detected}`
        : outcome === "flagged"
          ? `Flagged · ${detected} (not enforced)`
          : outcome === "held"
            ? "Held · scan unavailable"
            : "Not scanned";

  return (
    <motion.div
      variants={cardReveal}
      initial="initial"
      animate="animate"
      exit="exit"
      className={cn("card rounded-2xl p-4 ring-1 transition-shadow duration-500", t.ring)}
    >
      <div className="mb-3 border-b border-line pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-slate-100">
            {stepNum != null ? `${stepNum} · ${def.label}` : def.label}
          </span>
          <span className="flex items-center gap-1 rounded border border-line-bold bg-wash-strong px-1.5 py-0.5 font-mono text-[11px] font-medium text-slate-300">
            <ShieldGlyph />
            Model Armor
          </span>
          <div className="ml-auto flex items-center gap-2">
            {scanning ? (
              <LiveTimer since={activatedAt} />
            ) : (
              verdict?.latency_ms != null && (
                <span
                  className="rounded-md border border-line bg-wash px-2 py-0.5 font-mono text-[12px] font-medium text-live-300"
                  title="Time Model Armor took to scan"
                >
                  {formatLatency(verdict.latency_ms)}
                </span>
              )
            )}
            <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide", t.badge)}>
              {outcome ? OUTCOME_WORD[outcome].toUpperCase() : "SCANNING"}
            </span>
          </div>
        </div>
        <p className="mt-1 text-sm text-slate-300">{scanning ? copy.scanning : def.narrative}</p>
      </div>

      <div className="space-y-2.5">
        <Section label="Checks" subtitle={checksSubtitle} defaultOpen>
          {rows.length > 0 ? (
            <Checklist rows={rows} scanning={scanning} />
          ) : (
            <p className="px-2 py-1 text-xs text-slate-500">No filters ran, because the scan could not complete.</p>
          )}
        </Section>

        <Section label="Verdict" subtitle={verdictSubtitle} defaultOpen collapsible={false} tint={t.section}>
          <div className="flex items-start gap-4">
            <ShieldHero phase={phase} />
            <div className="min-w-0 flex-1 pt-1">
              <p className={cn("flex items-center text-[15px] font-semibold leading-tight", t.head)}>
                {headline}
                {scanning && <Ellipsis />}
              </p>
              {outcome ? (
                <>
                  <p className="mt-1.5 text-[13px] leading-snug text-slate-200">{outcome === "caught" && verdict ? armorCaughtMessage(verdict) : copy.result[outcome]}</p>
                  {verdict && <Meta verdict={verdict} />}
                </>
              ) : (
                <>
                  <p className="mt-1 text-xs text-slate-400">The result appears here as soon as Model Armor responds.</p>
                  <ScanBeam />
                </>
              )}
            </div>
          </div>
        </Section>
      </div>
    </motion.div>
  );
}

// ── Sections ────────────────────────────────────────────────────────────────

/** A titled block in the approval card's style: small-caps label, grey subtitle,
 * chevron to fold it. ``defaultOpen`` is re-applied when it changes, so a section
 * can open or fold itself while the reader can still toggle it. */
function Section({
  label,
  subtitle,
  defaultOpen,
  collapsible = true,
  tint,
  children,
}: {
  label: string;
  subtitle?: string;
  defaultOpen: boolean;
  collapsible?: boolean;
  tint?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => setOpen(defaultOpen), [defaultOpen]);
  const isOpen = !collapsible || open;
  const title = (
    <>
      {collapsible && (
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          className={cn("shrink-0 text-slate-400 transition-transform", isOpen && "rotate-90")}
        >
          <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
      <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">{label}</span>
      {subtitle && <span className="min-w-0 truncate text-xs text-slate-500">{subtitle}</span>}
    </>
  );
  return (
    <div className={cn("rounded-xl border", tint ?? "border-line-strong bg-wash")}>
      {collapsible ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={isOpen}
          className="flex w-full min-w-0 items-center gap-2.5 px-3.5 py-3 text-left"
        >
          {title}
        </button>
      ) : (
        <div className="flex min-w-0 items-center gap-2.5 px-3.5 pt-3">{title}</div>
      )}
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-3.5 pb-3 pt-1">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Checklist ───────────────────────────────────────────────────────────────

/** Advance a "now checking" cursor down the list while a scan runs; it rests on the
 * last row (all checked, waiting on the verdict) rather than looping. */
function useScanCursor(active: boolean, rows: number): number {
  const [cursor, setCursor] = useState(0);
  useEffect(() => {
    if (!active) return;
    setCursor(0);
    const id = setInterval(() => setCursor((c) => Math.min(c + 1, rows)), 340);
    return () => clearInterval(id);
  }, [active, rows]);
  return cursor;
}

function Checklist({ rows, scanning }: { rows: ArmorFilterRow[]; scanning: boolean }) {
  const cursor = useScanCursor(scanning, rows.length);
  return (
    <motion.ul
      key={scanning ? "scan" : "result"}
      variants={staggerParent}
      initial={scanning ? false : "initial"}
      animate="animate"
      className="space-y-1"
    >
      {rows.map((row, i) => (
        <motion.li
          key={row.key}
          variants={staggerItem}
          className={cn(
            "flex items-center gap-2.5 rounded-lg px-2 py-1.5 transition-colors duration-300",
            !scanning && row.result === "detected" && "bg-rose-500/[0.10]",
            !scanning && row.result === "allowed" && "bg-amber-400/[0.07]",
            scanning && i === cursor && "bg-brand-500/[0.08]",
          )}
        >
          <RowIcon
            state={scanning ? (i < cursor ? "checked" : i === cursor ? "checking" : "queued") : row.result}
          />
          <div className="min-w-0 flex-1">
            <p
              className={cn(
                "truncate text-[13px] leading-tight",
                scanning
                  ? i === cursor
                    ? "text-slate-100"
                    : i < cursor
                      ? "text-slate-400"
                      : "text-slate-500"
                  : row.result === "detected"
                    ? "font-semibold text-rose-200"
                    : row.result === "allowed"
                      ? "text-amber-200"
                      : "text-slate-200",
              )}
            >
              {row.label}
            </p>
            {!scanning && row.findings.length > 0 ? (
              <FindingLines row={row} />
            ) : (
              row.blurb && <p className="truncate text-[11px] leading-tight text-slate-500">{row.blurb}</p>
            )}
          </div>
          <span
            title={
              !scanning && row.result === "allowed"
                ? "Matched, but this filter is set to log only for inbound content (MODEL_ARMOR_INBOUND_IGNORE_FILTERS), so it does not block the invoice."
                : undefined
            }
            className={cn(
              "shrink-0 text-[11px] font-medium",
              scanning
                ? i === cursor
                  ? "text-brand-300"
                  : "text-slate-500"
                : row.result === "detected"
                  ? "text-rose-300"
                  : row.result === "allowed"
                    ? "text-amber-300"
                    : "text-done-300",
            )}
          >
            {scanning
              ? i === cursor
                ? "checking…"
                : i < cursor
                  ? "checked"
                  : "queued"
              : row.result === "detected"
                ? "Detected"
                : row.result === "allowed"
                  ? "Detected · not blocked"
                  : "Clear"}
          </span>
        </motion.li>
      ))}
    </motion.ul>
  );
}

/** Why a flagged filter fired: one plain-text line per place it was found. Rendered
 * as text only (React escapes it) and never as a link, even for a matched URL. */
function FindingLines({ row }: { row: ArmorFilterRow }) {
  return (
    <div className="space-y-0.5">
      {row.findings.slice(0, 4).map((f, i) => {
        const d = describeFinding(f);
        return (
          <p
            key={i}
            className={cn(
              "break-words text-[11px] leading-snug",
              row.result === "detected" ? "text-rose-300/90" : "text-amber-300/80",
            )}
          >
            {d.what && <span className={d.mono ? "break-all font-mono" : undefined}>{d.what}</span>}
            {d.what && d.source && " · "}
            {d.source && <span className="text-slate-400">Found in {d.source}</span>}
          </p>
        );
      })}
    </div>
  );
}

type IconState = "queued" | "checking" | "checked" | ArmorFilterRow["result"];

function RowIcon({ state }: { state: IconState }) {
  const base = "grid h-5 w-5 shrink-0 place-items-center rounded-full";
  if (state === "checking") {
    return (
      <span className={base}>
        <motion.span
          className="block h-3.5 w-3.5 rounded-full border-2 border-brand-400/30 border-t-brand-300"
          animate={{ rotate: 360 }}
          transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }}
        />
      </span>
    );
  }
  if (state === "queued") {
    return (
      <span className={base}>
        <span className="block h-1.5 w-1.5 rounded-full bg-slate-600" />
      </span>
    );
  }
  const glyph = { stroke: "white", strokeWidth: 3, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" } as const;
  const bg =
    state === "checked"
      ? "bg-wash-strong"
      : state === "clear"
        ? "bg-done-500"
        : state === "detected"
          ? "bg-rose-500"
          : "bg-amber-500";
  return (
    <motion.span
      className={cn(base, bg)}
      initial={state === "checked" ? false : { scale: 0 }}
      animate={{ scale: 1 }}
      transition={{ type: "spring", stiffness: 520, damping: 18 }}
    >
      <svg width="11" height="11" viewBox="0 0 24 24">
        {state === "detected" ? (
          <path d="M6 6l12 12M18 6L6 18" {...glyph} />
        ) : state === "allowed" ? (
          <path d="M12 6v8M12 18.4v.2" {...glyph} />
        ) : (
          <path d="M5 13l4 4L19 7" {...glyph} stroke={state === "checked" ? "#94a3b8" : "white"} />
        )}
      </svg>
    </motion.span>
  );
}

// ── Result details ──────────────────────────────────────────────────────────

function Meta({ verdict }: { verdict: ArmorVerdict }) {
  return (
    <div className="mt-2 space-y-1.5 text-[11px] text-slate-400">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="rounded-md border border-line bg-wash px-1.5 py-0.5">
          {verdict.enforce ? "Enforcing" : "Inspect-only"}
        </span>
      </div>
      {!verdict.inputs?.length && verdict.notes.map((n) => <p key={n}>• {n}</p>)}
      {verdict.error && <p className="line-clamp-2 font-mono text-amber-300/90">Detail: {verdict.error}</p>}
    </div>
  );
}

// ── Visuals ─────────────────────────────────────────────────────────────────

/** Live stopwatch while a scan runs, so it reads as in progress, not stuck. */
function LiveTimer({ since }: { since?: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, []);
  const ms = since ? Math.max(0, now - since) : 0;
  return (
    <span className="rounded-md border border-brand-400/25 bg-brand-500/10 px-2 py-0.5 font-mono text-[12px] font-medium text-brand-200">
      {(ms / 1000).toFixed(1)}s
    </span>
  );
}

function Ellipsis() {
  return (
    <span className="ml-0.5 inline-flex">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          animate={{ opacity: [0.2, 1, 0.2] }}
          transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.2 }}
        >
          .
        </motion.span>
      ))}
    </span>
  );
}

/** Indeterminate sweep under the headline while the scan runs. */
function ScanBeam() {
  return (
    <div className="relative mt-2.5 h-1 overflow-hidden rounded-full bg-wash-strong">
      <motion.div
        className="absolute inset-y-0 w-1/3 rounded-full bg-gradient-to-r from-transparent via-brand-400 to-transparent"
        animate={{ x: ["-100%", "300%"] }}
        transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
      />
    </div>
  );
}

const SHIELD_48 = "M24 3.5 L41.5 9.6 V23 C41.5 33.2 34.6 41 24 45 C13.4 41 6.5 33.2 6.5 23 V9.6 Z";

/** The hero shield: sweeps and pings while scanning, then lands as a check, cross or alert. */
function ShieldHero({ phase }: { phase: Phase }) {
  const uid = useId().replace(/:/g, "");
  const t = TONE[phase];
  const glyph = { stroke: "white", strokeWidth: 3.4, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" } as const;
  return (
    <div className="relative grid h-16 w-16 shrink-0 place-items-center">
      {phase === "scanning" && (
        <>
          <span className="pointer-events-none absolute inset-1 rounded-full border border-brand-400/50 anim-sonar" />
          <span className="pointer-events-none absolute inset-1 rounded-full border border-brand-400/50 anim-sonar [animation-delay:1.5s]" />
        </>
      )}
      <motion.svg
        key={phase}
        width="54"
        height="54"
        viewBox="0 0 48 48"
        initial={phase === "scanning" ? false : { scale: 0.7, opacity: 0 }}
        animate={phase === "stop" ? { scale: 1, opacity: 1, x: [0, -5, 5, -4, 4, 0] } : { scale: 1, opacity: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 16 }}
        style={{ filter: `drop-shadow(0 0 8px ${t.fill})` }}
      >
        <defs>
          <clipPath id={`${uid}c`}>
            <path d={SHIELD_48} />
          </clipPath>
          <linearGradient id={`${uid}g`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#a78bfa" stopOpacity="0" />
            <stop offset="1" stopColor="#a78bfa" stopOpacity="0.75" />
          </linearGradient>
        </defs>
        <path d={SHIELD_48} fill={t.fill} stroke={t.stroke} strokeWidth="2.2" strokeLinejoin="round" />
        {phase === "scanning" && (
          <g clipPath={`url(#${uid}c)`}>
            <motion.rect
              x="0"
              y="-14"
              width="48"
              height="14"
              fill={`url(#${uid}g)`}
              animate={{ y: [-14, 48] }}
              transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
            />
          </g>
        )}
        {phase === "scanning" && <circle cx="24" cy="24" r="3" fill="#ddd6fe" />}
        {phase === "pass" && (
          <motion.path
            d="M15.5 24.5l6 6 11-12.5"
            {...glyph}
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={{ duration: 0.4, delay: 0.1 }}
          />
        )}
        {phase === "stop" && <path d="M17 17l14 14M31 17L17 31" {...glyph} />}
        {phase === "warn" && (
          <>
            <path d="M24 15v11" {...glyph} />
            <circle cx="24" cy="32" r="1.4" fill="white" stroke="white" strokeWidth="1" />
          </>
        )}
      </motion.svg>
    </div>
  );
}

function ShieldGlyph() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinejoin="round">
      <path d="M12 2.5 L20 5.5 V11.6 C20 16.4 16.6 20.2 12 21.8 C7.4 20.2 4 16.4 4 11.6 V5.5 Z" />
    </svg>
  );
}
