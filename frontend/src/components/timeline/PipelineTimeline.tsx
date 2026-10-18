/**
 * Agentic Pipeline — an animated directed graph that mirrors the backend ADK
 * workflow (app/agent.py). Rendered as an indented flow tree: a left spine
 * carries the happy path; branches (human approval, rejection-email loop,
 * escalation, ERP posting) indent to the right. Every connector is routed
 * through the left gutters so lines never cross a node box. Inactive routes
 * are dimmed; the path the run takes lights up and fills emerald as each stage
 * completes (LangGraph-Studio style).
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore, type StageStatus } from "../../store/usePipelineStore";
import { STAGES, isArmorStage, type ArmorStageId, type StageId } from "../../store/stageMap";
import { useModelConfig } from "../../store/useModelConfig";
import { useSecurityConfig } from "../../store/useSecurityConfig";
import {
  FILTER_SHORT,
  OUTCOME_CHIP,
  armorOutcome,
  armorRailSummary,
  armorScanningFilters,
  armorVerdict,
  outcomeTone,
  type ArmorTone,
} from "../../lib/armor";
import { cn } from "../../lib/utils";
import { AgentGlyph } from "../common/AgentGlyph";
import { useWorkingLabel } from "../../hooks/useWorkingLabel";
import { stagesWithCards } from "../../lib/stageCards";
import { jumpToStage } from "../../lib/feedNav";
import { deriveRunCost } from "../../lib/runCost";
import { formatLatency } from "../../lib/duration";

/** "1.8s" for >=1s, else "940ms". */
function fmtLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

type Kind = "node" | "agent" | "human" | "decision" | "terminal" | "armor";

interface GNode {
  id: StageId;
  depth: number; // 0 = spine, 1/2 = branch indentation
  parent: StageId | null;
  kind: Kind;
  label?: string; // route label that led INTO this node
  loop?: boolean; // shows the "revise ↺" cycle hint
}

// DFS execution order — each subtree is contiguous so gutter rails never cross
// a sibling box. depth controls horizontal indentation. The armor_* rows are the
// Model Armor scan steps; they are dropped (and their children re-parented) when
// Model Armor is off, see buildLayout.
const ALL_NODES: GNode[] = [
  { id: "intake", depth: 0, parent: null, kind: "node" },
  { id: "armor_intake", depth: 0, parent: "intake", kind: "armor" },
  { id: "screening", depth: 0, parent: "armor_intake", kind: "agent" },
  { id: "screening_decision", depth: 0, parent: "screening", kind: "decision" },
  { id: "routing", depth: 0, parent: "screening_decision", kind: "decision" },
  { id: "extraction", depth: 0, parent: "routing", kind: "agent" },
  { id: "classify", depth: 0, parent: "extraction", kind: "decision" },
  { id: "validation", depth: 0, parent: "classify", kind: "decision" },
  { id: "approval", depth: 1, parent: "validation", kind: "human", label: "review" },
  { id: "draft_email", depth: 2, parent: "approval", kind: "agent", label: "reject" },
  { id: "review_email", depth: 2, parent: "draft_email", kind: "human", loop: true },
  { id: "notify", depth: 2, parent: "review_email", kind: "node", label: "send" },
  { id: "escalate", depth: 1, parent: "validation", kind: "node", label: "duplicate" },
  { id: "post_erp", depth: 1, parent: "validation", kind: "node", label: "post to ERP" },
  { id: "complete", depth: 0, parent: "validation", kind: "terminal" },
];
const ALL_BY_ID = Object.fromEntries(ALL_NODES.map((n) => [n.id, n])) as Record<StageId, GNode>;

// Geometry (px). Deterministic — no DOM measurement needed for Y.
const ROW_H = 60;
const NODE_H = 46;
const BASE_X = 10; // left padding before the first marker
const INDENT = 22; // px added per depth level
const MARKER_R = 12; // marker radius (24px box)
const RIGHT_PAD = 6;

interface Layout {
  nodes: GNode[];
  row: Partial<Record<StageId, number>>;
  byId: Partial<Record<StageId, GNode>>;
  totalH: number;
}

const LAYOUTS: Record<"plain" | "armor", Layout> = {
  plain: buildLayout(false),
  armor: buildLayout(true),
};

function buildLayout(armorOn: boolean): Layout {
  const nodes = armorOn
    ? ALL_NODES
    : ALL_NODES.filter((n) => !isArmorStage(n.id)).map((n) => {
        // Splice a hidden scan row out of the chain: its child hangs off its parent.
        let parent = n.parent;
        while (parent && isArmorStage(parent)) parent = ALL_BY_ID[parent].parent;
        return parent === n.parent ? n : { ...n, parent };
      });
  return {
    nodes,
    row: Object.fromEntries(nodes.map((n, i) => [n.id, i])),
    byId: Object.fromEntries(nodes.map((n) => [n.id, n])),
    totalH: nodes.length * ROW_H,
  };
}

const markerX = (depth: number) => BASE_X + depth * INDENT + MARKER_R;
const cardLeft = (depth: number) => BASE_X + depth * INDENT; // marker sits at card's left
const rowTop = (l: Layout, id: StageId) => (l.row[id] ?? 0) * ROW_H;
const markerY = (l: Layout, id: StageId) => (l.row[id] ?? 0) * ROW_H + NODE_H / 2;

type EdgeStatus = "pending" | "active" | "done";

export function PipelineTimeline() {
  const stageStatus = usePipelineStore((s) => s.stageStatus);
  const state = usePipelineStore((s) => s.state);
  const items = usePipelineStore((s) => s.items);
  const cardStages = useMemo(() => stagesWithCards(items, state), [items, state]);
  const confirmedModels = useModelConfig((s) => s.confirmed);
  // Computed once here (not inside GraphNode) so only one interval ticks
  // while intake is active, not one per rendered node.
  const intakeLiveLabel = useWorkingLabel("intake");

  // The Model Armor steps show when the backend has it on, or as soon as a run
  // reports one of its verdicts (covers a failed config fetch).
  const armorConfigured = useSecurityConfig((s) => s.modelArmorEnabled);
  useEffect(() => {
    void useSecurityConfig.getState().load();
  }, []);
  const layout = armorConfigured || state.armor_intake ? LAYOUTS.armor : LAYOUTS.plain;

  const wrapRef = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setW(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const reached = (id: StageId) => stageStatus[id] != null;
  const vroute = state.validation_route;
  const poRoute = state.po_match?.route;

  // Is this node's incoming (parent→node) edge on the route the run took?
  const onPath = (id: StageId): boolean => {
    switch (id) {
      case "approval":
        return vroute === "human_review" || reached("approval");
      case "escalate":
        return vroute === "duplicate_po";
      case "post_erp":
        return reached("post_erp") || (vroute === "proceed" && (poRoute === "direct" || poRoute === "indirect"));
      case "complete":
        return (
          reached("complete") &&
          (reached("post_erp") || reached("escalate") || reached("notify"))
        );
      default:
        return reached(id);
    }
  };

  const edgeStatus = (id: StageId): EdgeStatus => {
    if (!onPath(id)) return "pending";
    const ts = stageStatus[id];
    if (ts === "done") return "done";
    if (ts === "active") return "active";
    return "pending";
  };

  return (
    <div className="card rounded-2xl p-5">
      <div className="mb-5 text-center">
        <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
          Agentic Workflow
        </h2>
      </div>

      <div ref={wrapRef} className="relative mx-auto w-full" style={{ height: layout.totalH }}>
        {/* ── Edge layer (routed through left gutters) ─────────────────── */}
        {w > 0 && (
          <svg
            className="pointer-events-none absolute inset-0"
            width={w}
            height={layout.totalH}
            viewBox={`0 0 ${w} ${layout.totalH}`}
            fill="none"
          >
            {/* dim topology track — always present. Several edges share the same
                left-gutter rail (e.g. validation → each branch → complete), so the
                strokes are solid and the opacity is applied to the GROUP: overlapping
                rails composite once and read as a single, uniform line. */}
            <g
              stroke="var(--track)"
              strokeWidth={1.5}
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ opacity: "var(--track-opacity)" }}
            >
              {layout.nodes.filter((n) => n.parent).map((n) => (
                <path key={`track-${n.id}`} d={edgePath(layout, n)} />
              ))}
            </g>
            {/* live overlays — flowing dashes while active, emerald sweep on done */}
            {layout.nodes.filter((n) => n.parent).map((n) => {
              if (edgeStatus(n.id) !== "active") return null;
              return (
                <path
                  key={`flow-${n.id}`}
                  className="edge-flow"
                  d={edgePath(layout, n)}
                  stroke="var(--color-brand-400)"
                  strokeWidth={2.5}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              );
            })}
            {/* done — re-mount (keyed) so the fill sweep replays on transition. The edge
                into a scan that stopped the content is drawn red, not green. The glow is
                applied per GROUP (not per path) so shared rails don't stack it and look
                bolder than the rest. */}
            {(["pass", "stop"] as const).map((kind) => (
              <g
                key={`done-${kind}`}
                style={{
                  filter:
                    kind === "stop"
                      ? "drop-shadow(0 0 3px rgba(251,113,133,0.55))"
                      : "drop-shadow(0 0 3px rgba(16,185,129,0.55))",
                }}
              >
                {layout.nodes
                  .filter((n) => n.parent && edgeStatus(n.id) === "done")
                  .filter((n) => (isArmorStage(n.id) && armorTone(n.id, state) === "stop") === (kind === "stop"))
                  .map((n) => (
                    <motion.path
                      key={`done-${n.id}`}
                      d={edgePath(layout, n)}
                      stroke={kind === "stop" ? "#fb7185" : "var(--color-done-500)"}
                      strokeWidth={2.75}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      initial={{ pathLength: 0, opacity: 0.85 }}
                      animate={{ pathLength: 1, opacity: 1 }}
                      transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
                    />
                  ))}
              </g>
            ))}
          </svg>
        )}

        {/* ── Node layer ───────────────────────────────────────────────── */}
        {w > 0 &&
          layout.nodes.map((n) => {
            const status: StageStatus = stageStatus[n.id] ?? "pending";
            const left = cardLeft(n.depth);
            return (
              <GraphNode
                key={n.id}
                node={n}
                status={status}
                left={left}
                width={w - left - RIGHT_PAD}
                top={rowTop(layout, n.id)}
                target={cardStages.has(n.id) || (n.id === "complete" && !!state.pipeline_status)}
                tipAbove={(layout.row[n.id] ?? 0) >= layout.nodes.length - 3}
                state={state}
                confirmedModels={confirmedModels}
                intakeLiveLabel={intakeLiveLabel}
              />
            );
          })}
      </div>
    </div>
  );
}

/** Elbow path from a parent marker down through the gutter into the child marker. */
function edgePath(l: Layout, n: GNode): string {
  const parent = l.byId[n.parent as StageId] as GNode;
  const px = markerX(parent.depth);
  const cxv = markerX(n.depth);
  const y1 = markerY(l, parent.id) + MARKER_R;
  const cy = markerY(l, n.id);
  if (Math.abs(px - cxv) < 0.5) {
    // same depth → straight vertical trunk into the child marker top
    return `M ${px} ${y1} L ${cxv} ${cy - MARKER_R}`;
  }
  // indent → down the parent's gutter rail, then a short stub into the child
  return `M ${px} ${y1} L ${px} ${cy} L ${cxv - MARKER_R} ${cy}`;
}

/** How a finished scan reads on the rail: green passed, red stopped, amber needs a look. */
function armorTone(id: StageId, state: ReturnType<typeof usePipelineStore.getState>["state"]): ArmorTone | undefined {
  return isArmorStage(id) ? outcomeTone(armorOutcome(armorVerdict(id, state))) : undefined;
}

interface GraphNodeProps {
  node: GNode;
  status: StageStatus;
  width: number;
  left: number;
  top: number;
  /** A feed card exists for this stage, so the node is clickable. */
  target: boolean;
  /** Show the tooltip above the node (near the bottom of the rail). */
  tipAbove: boolean;
  state: ReturnType<typeof usePipelineStore.getState>["state"];
  confirmedModels: Record<string, string>;
  intakeLiveLabel: string;
}

/** Route chip + one-line policy note for the deterministic screening decision,
 * derived from the backend's authoritative route and the evidence counts. */
function screeningDecisionView(
  state: GraphNodeProps["state"],
): { routeLabel?: string; note?: string } {
  const route = state.screening_route;
  if (!route) return {};
  const a = state.screening_assessment;
  const strengths = a ? [a.attachment_signal, a.body_signal, a.subject_signal] : [];
  const strong = strengths.filter((s) => s === "strong").length;
  const weak = strengths.filter((s) => s === "weak").length;
  if (route === "invoice") {
    return { routeLabel: "Invoice →", note: `${strong} strong signals` };
  }
  const note =
    strong || weak
      ? `${strong} strong · ${weak} weak — insufficient corroboration`
      : "no invoice signals";
  return { routeLabel: "Not an invoice", note };
}

const STRATEGY_LABEL: Record<string, string> = {
  inline: "Inline",
  files_api: "Files API",
  multipass: "Multipass",
};
const TIER_LABEL: Record<string, string> = { lite: "Lite", flash: "Flash", pro: "Pro" };

/** Route chip + one-line note for the deterministic extraction-strategy router
 * — leads with the recommended model tier (the outcome that matters), with a
 * "(not applied)" caveat when dynamic tiering is off, derived from the
 * backend's route_extraction_strategy state. Mirrors the fuller explanation in
 * the routing StepCard. */
function routingDecisionView(
  state: GraphNodeProps["state"],
): { routeLabel?: string; note?: string } {
  const tier = state.extraction_recommended_model;
  if (!tier) return {};
  const strategy = state.extraction_strategy;
  const applied = state.extraction_dynamic_tiering_enabled;
  const transportNote = strategy && strategy !== "inline" ? `${STRATEGY_LABEL[strategy] ?? strategy} · ` : "";
  return {
    routeLabel: TIER_LABEL[tier] ?? tier,
    note: `${transportNote}recommended${applied === false ? " (not applied)" : ""}`,
  };
}

function GraphNode({
  node,
  status,
  width,
  left,
  top,
  target,
  tipAbove,
  state,
  confirmedModels,
  intakeLiveLabel,
}: GraphNodeProps) {
  const def = STAGES[node.id];
  const done = status === "done";
  const active = status === "active";
  const ref = useRef<HTMLDivElement>(null);
  const tip = useHoverIntent();
  // Keep the live step in view: with the Model Armor rows the rail outgrows the pane.
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [active]);
  const armorStage = isArmorStage(node.id) ? node.id : null;
  const verdict = armorStage ? armorVerdict(armorStage, state) : undefined;
  const outcome = armorOutcome(verdict);
  // Only meaningful once the scan finished; a running or queued scan stays neutral.
  const tone: ArmorTone | undefined = armorStage && done ? outcomeTone(outcome) : undefined;
  // The deterministic decision node shows a live route chip (proceed/discard)
  // + a one-line policy note, sourced from state rather than re-deriving here.
  const decision =
    node.id === "screening_decision" ? screeningDecisionView(state)
    : node.id === "routing" ? routingDecisionView(state)
    : undefined;
  const routeLabel = decision?.routeLabel ?? node.label;
  // Deterministic stages that time a real external write (ERP posting)
  // show that latency next to "Done", like the agent stages do via AgentMeta.
  const stageMs = !def.agent && def.latencyKey ? state[def.latencyKey] : undefined;

  // `done` wins over `def.agent`: a completed stage is FULLY green regardless of
  // kind, so agent stages don't leave a blue tint blending with the green (no
  // two-colour merge). Agents are blue only while active/pending. Flat fills —
  // no glow shadows — for a sober, production-grade read.
  const container = done
    ? tone === "stop"
      ? "border-rose-400/55 bg-rose-500/[0.10]"
      : tone === "warn"
        ? "border-amber-400/50 bg-amber-400/[0.08]"
        : "border-done-500/55 bg-done-500/[0.10]"
    : def.agent
      ? active
        ? "border-brand-400/60 bg-brand-500/[0.12] shadow-[0_8px_26px_-12px_var(--color-brand-500)]"
        : "border-brand-400/55 bg-brand-500/[0.08]"
      : active
        ? "border-brand-400/60 bg-brand-500/[0.12] shadow-[0_8px_26px_-12px_var(--color-brand-500)]"
        : "border-line-strong bg-wash opacity-80";

  return (
    <div
      ref={ref}
      className="absolute"
      style={{ left, top, width, height: NODE_H }}
      onMouseEnter={tip.show}
      onMouseLeave={tip.hide}
      onFocus={tip.show}
      onBlur={tip.hide}
      {...(target
        ? {
            role: "button",
            tabIndex: 0,
            "aria-label": `${def.label}: show its card`,
            onClick: () => jumpToStage(node.id),
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                jumpToStage(node.id);
              }
            },
          }
        : {})}
    >
    <motion.div
      className={cn(
        "flex h-full w-full items-center gap-2.5 overflow-hidden rounded-xl border pl-1.5 pr-2.5 transition-colors duration-500",
        target && "cursor-pointer hover:border-brand-400/80",
        container,
      )}
    >
      {armorStage ? (
        <ShieldMarker status={status} tone={tone} />
      ) : (
        <NodeMarker kind={node.kind} status={status} />
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          {def.agent && <AgentGlyph size={20} animate={active} />}
          {node.kind === "human" && <HumanIcon />}
          <p
            className={cn(
              "truncate text-[13px] font-medium leading-tight",
              done ? TONE_TEXT[tone ?? "pass"].title : active ? "text-slate-50" : "text-slate-200",
            )}
          >
            {def.label}
          </p>
        </div>
        {def.agent && def.model && (active || done) ? (
          <AgentMeta def={def} state={state} confirmedModels={confirmedModels} done={done} />
        ) : (
          <p
            className={cn(
              "truncate text-[11px] leading-tight",
              done ? TONE_TEXT[tone ?? "pass"].sub : active ? "text-brand-200" : "text-slate-400",
            )}
          >
            {decision?.note && (active || done)
              ? decision.note
              : armorStage && active
                ? <ArmorScanLine stage={armorStage} />
                : armorStage && done
                  ? armorRailSummary(verdict)
                  : done
                    ? stageMs != null
                      ? `Done · ${fmtLatency(stageMs)}`
                      : "Done"
                    : node.id === "intake" && active
                      ? intakeLiveLabel
                      : def.hint}
          </p>
        )}
      </div>

      {/* right-side chip — AGENT type badge for agent stages, else route badge */}
      {(def.agent || armorStage || routeLabel || node.loop) && (
        <div className="flex shrink-0 flex-col items-end gap-0.5">
          {def.agent ? (
            <AgentChip />
          ) : armorStage ? (
            <ArmorChip
              label={done && outcome ? OUTCOME_CHIP[outcome] : active ? "Scanning" : "Security"}
              tone={tone}
              active={active}
            />
          ) : (
            <>
              {routeLabel && <RouteChip label={routeLabel} />}
              {node.loop && <RouteChip label="revise ↺" />}
            </>
          )}
        </div>
      )}
    </motion.div>
    {tip.open && (
      <NodeTooltip
        node={node}
        status={status}
        state={state}
        confirmedModels={confirmedModels}
        target={target}
        above={tipAbove}
      />
    )}
    </div>
  );
}

/** Open after a short dwell (avoids flicker while the pointer crosses the rail). */
function useHoverIntent(delayMs = 260) {
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return {
    open,
    show: () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setOpen(true), delayMs);
    },
    hide: () => {
      if (timer.current) clearTimeout(timer.current);
      setOpen(false);
    },
  };
}

const STATUS_WORD: Record<StageStatus, string> = { pending: "Waiting", active: "Running", done: "Done" };
const TIP_PREFIX: Partial<Record<StageId, "screening" | "extraction" | "draft_email">> = {
  screening: "screening",
  extraction: "extraction",
  draft_email: "draft_email",
};

function TipRow({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <dt className="text-slate-400">{k}</dt>
      <dd className="min-w-0 truncate text-right font-mono text-slate-100">{v}</dd>
    </div>
  );
}

/** Hover card for a graph node: what the step is, how long it took, and, for
 * agent steps, the model, tokens and cost. All from state already in the UI. */
function NodeTooltip({
  node,
  status,
  state,
  confirmedModels,
  target,
  above,
}: {
  node: GNode;
  status: StageStatus;
  state: GraphNodeProps["state"];
  confirmedModels: Record<string, string>;
  target: boolean;
  above: boolean;
}) {
  const def = STAGES[node.id];
  const prefix = TIP_PREFIX[node.id];
  const ms = def.latencyKey ? (state[def.latencyKey] as number | undefined) : undefined;
  const model = def.agent
    ? ((def.modelKey ? state[def.modelKey] : undefined) ?? (prefix ? confirmedModels[prefix] : undefined) ?? def.model)
    : undefined;
  const inTok = prefix ? state[`${prefix}_input_tokens`] : undefined;
  const outTok = prefix ? state[`${prefix}_output_tokens`] : undefined;
  const thinkTok = prefix ? state[`${prefix}_thinking_tokens`] : undefined;
  const stepCost = prefix ? deriveRunCost(state).steps.find((s) => s.key === prefix)?.cost : undefined;

  return (
    <div
      role="tooltip"
      className={cn(
        "tip-in card pointer-events-none absolute left-3 z-40 w-64 rounded-xl p-3 shadow-[var(--shadow-raised)]",
        above ? "bottom-full mb-1.5" : "top-full mt-1.5",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-semibold text-slate-50">{def.label}</p>
        <span className="rounded-full border border-line-strong bg-wash px-2 py-0.5 text-[11px] font-medium text-slate-300">
          {STATUS_WORD[status]}
        </span>
      </div>
      <p className="mt-1 text-xs leading-snug text-slate-400">{def.hint}</p>
      {(model || ms != null || inTok != null) && (
        <dl className="mt-2 space-y-1 border-t border-line pt-2">
          {model && <TipRow k="Model" v={model} />}
          {ms != null && <TipRow k="Latency" v={formatLatency(ms)} />}
          {inTok != null && (
            <TipRow
              k="Tokens"
              v={`${inTok.toLocaleString()} in · ${(outTok ?? 0).toLocaleString()} out${thinkTok ? ` · ${thinkTok.toLocaleString()} think` : ""}`}
            />
          )}
          {stepCost != null && stepCost > 0 && <TipRow k="Cost" v={`$${stepCost.toFixed(4)}`} />}
        </dl>
      )}
      {target && <p className="mt-2 text-[11px] font-medium text-brand-300">Click to jump to its card</p>}
    </div>
  );
}

// Text colours for a finished row (title / sub-line); plain stages read as "pass".
const TONE_TEXT: Record<ArmorTone, { title: string; sub: string }> = {
  pass: { title: "text-done-300", sub: "text-done-500/90" },
  stop: { title: "text-rose-200", sub: "text-rose-300/90" },
  warn: { title: "text-amber-200", sub: "text-amber-300/90" },
};

/** Live sub-line for a running Model Armor scan: steps through the filters being applied. */
function ArmorScanLine({ stage }: { stage: ArmorStageId }) {
  const filters = armorScanningFilters(stage);
  const [i, setI] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setI((n) => (n + 1) % filters.length), 650);
    return () => clearInterval(id);
  }, [filters.length]);
  return <>Checking {FILTER_SHORT[filters[i].key] ?? filters[i].label.toLowerCase()}…</>;
}

/** Right-hand badge of a Model Armor row: its state while running, its outcome after. */
function ArmorChip({ label, tone, active }: { label: string; tone?: ArmorTone; active: boolean }) {
  const cls = tone
    ? tone === "stop"
      ? "border-rose-400/45 bg-rose-500/15 text-rose-200"
      : tone === "warn"
        ? "border-amber-400/45 bg-amber-400/15 text-amber-200"
        : "border-done-500/45 bg-done-500/15 text-done-300"
    : active
      ? "border-brand-400/45 bg-brand-500/15 text-brand-200"
      : "border-line-strong bg-wash text-slate-400";
  return (
    <span
      className={cn(
        "whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
        cls,
      )}
    >
      {label}
    </span>
  );
}

/** Static AGENT badge — marks stages backed by an ADK agent. */
function AgentChip() {
  return (
    <span className="whitespace-nowrap rounded-full border border-brand-400/45 bg-brand-500/15 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-brand-200">
      Agent
    </span>
  );
}

// Route chips are intentionally monochrome: AGENT (AgentChip) is the single
// colored accent so the agent stages are the only thing that pops. Every other
// route reads as a neutral label.
function RouteChip({ label }: { label: string }) {
  return (
    <span className="whitespace-nowrap rounded-full border border-line-strong bg-wash px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
      {label}
    </span>
  );
}

function AgentMeta({
  def,
  state,
  confirmedModels,
  done,
}: {
  def: (typeof STAGES)[StageId];
  state: GraphNodeProps["state"];
  confirmedModels: Record<string, string>;
  done: boolean;
}) {
  const ms = def.latencyKey ? state[def.latencyKey] : undefined;
  const runModel = def.modelKey ? state[def.modelKey] : undefined;
  const agentKey =
    def.modelKey === "draft_email_model" ? "draft_email"
    : def.modelKey === "screening_model" ? "screening"
    : "extraction";
  const model = runModel ?? confirmedModels[agentKey] ?? def.model;
  const liveLabel = useWorkingLabel(def.id);
  return (
    <p className="mt-px flex items-center gap-1 truncate font-mono text-[11px] text-slate-400">
      <span className="text-brand-200">{model}</span>
      <span className="text-slate-600">·</span>
      {ms != null ? (
        <span className="text-done-300">{fmtLatency(ms)}</span>
      ) : done ? (
        <span className="text-slate-500">—</span>
      ) : (
        <span className="truncate text-slate-400">{liveLabel}</span>
      )}
    </p>
  );
}

/** Status marker — a dot, a diamond (decision), or a ringed glyph. */
function NodeMarker({ kind, status }: { kind: Kind; status: StageStatus }) {
  const done = status === "done";
  const active = status === "active";
  const isDiamond = kind === "decision";

  const fill = done
    ? "bg-done-500 border-transparent"
    : active
      ? "bg-brand-500 border-transparent"
      : "border-line-bold bg-wash-strong";

  return (
    <div className="relative z-10 shrink-0">
      <AnimatePresence>
        <motion.div
          key={done ? "done" : active ? "active" : "pending"}
          className={cn(
            "relative grid h-6 w-6 place-items-center border transition-colors",
            fill,
            isDiamond ? "rotate-45 rounded-[5px]" : "rounded-full",
            active && (done ? "ring-4 ring-done-500/40" : "ring-4 ring-brand-500/40"),
          )}
          animate={active ? { scale: [1, 1.15, 1] } : { scale: 1 }}
          transition={
            active
              ? { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
              : { duration: 0.2 }
          }
        >
          <span className={isDiamond ? "-rotate-45" : undefined}>
            {done ? (
              <motion.span
                className="block"
                initial={{ scale: 0, rotate: -30 }}
                animate={{ scale: 1, rotate: 0 }}
                transition={{ type: "spring", stiffness: 520, damping: 18 }}
              >
                <CheckIcon />
              </motion.span>
            ) : (
              <span
                className={cn(
                  "block h-1.5 w-1.5 rounded-full",
                  active ? "bg-white" : "bg-slate-400",
                )}
              />
            )}
          </span>
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

const SHIELD_PATH = "M12 1.5 L21 4.8 V11.6 C21 17 17.2 21 12 22.8 C6.8 21 3 17 3 11.6 V4.8 Z";
const SHIELD_PALETTE = {
  pending: { fill: "var(--c-wash-strong)", stroke: "var(--c-line-bold)" },
  active: { fill: "#7c5cf5", stroke: "#a78bfa" },
  pass: { fill: "#10b981", stroke: "#34d399" },
  stop: { fill: "#f43f5e", stroke: "#fb7185" },
  warn: { fill: "#f59e0b", stroke: "#fbbf24" },
};

/** Status marker for a Model Armor scan: a shield that pulses while scanning, then
 * resolves to a green check (passed), red cross (caught) or amber mark (needs a look). */
function ShieldMarker({ status, tone }: { status: StageStatus; tone?: ArmorTone }) {
  const done = status === "done";
  const active = status === "active";
  const palette = SHIELD_PALETTE[done ? (tone ?? "pass") : active ? "active" : "pending"];
  const glyph = { stroke: "white", strokeWidth: 2.4, strokeLinecap: "round", strokeLinejoin: "round", fill: "none" } as const;
  return (
    <div className="relative z-10 shrink-0">
      <motion.div
        key={done ? `done-${tone}` : active ? "active" : "pending"}
        className="grid h-6 w-6 place-items-center"
        style={active ? { filter: "drop-shadow(0 0 5px rgba(167,139,250,0.7))" } : undefined}
        initial={done ? { scale: 0.6 } : false}
        animate={
          active
            ? { scale: [1, 1.15, 1] }
            : done && tone === "stop"
              ? { scale: 1, x: [0, -2.5, 2.5, -2, 2, 0] }
              : { scale: 1 }
        }
        transition={
          active
            ? { duration: 1.6, repeat: Infinity, ease: "easeInOut" }
            : { type: "spring", stiffness: 420, damping: 16 }
        }
      >
        <svg width="24" height="24" viewBox="0 0 24 24">
          <path d={SHIELD_PATH} fill={palette.fill} stroke={palette.stroke} strokeWidth="1.4" strokeLinejoin="round" />
          {done && tone === "stop" && <path d="M8.8 8.8l6.4 6.4M15.2 8.8l-6.4 6.4" {...glyph} />}
          {done && tone === "warn" && (
            <>
              <path d="M12 7.6v5" {...glyph} />
              <circle cx="12" cy="16.2" r="0.6" fill="white" stroke="white" strokeWidth="1" />
            </>
          )}
          {done && tone !== "stop" && tone !== "warn" && <path d="M8 12.4l3 3 5.2-6" {...glyph} />}
          {!done && <circle cx="12" cy="12" r="2" fill={active ? "white" : "#94a3b8"} />}
        </svg>
      </motion.div>
    </div>
  );
}

function HumanIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.8" className="text-amber-300">
      <circle cx="12" cy="8" r="3.4" />
      <path d="M5 20a7 7 0 0 1 14 0" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3.5">
      <motion.path
        d="M5 13l4 4L19 7"
        strokeLinecap="round"
        strokeLinejoin="round"
        initial={{ pathLength: 0 }}
        animate={{ pathLength: 1 }}
        transition={{ duration: 0.3 }}
      />
    </svg>
  );
}
