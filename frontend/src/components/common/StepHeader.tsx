import { AgentGlyph } from "./AgentGlyph";

/** Shared step header — number, title, Deterministic/Agent badge, and a
 * one-line human-tone narrative. The single rendering used by every step in
 * the pipeline, whether it's backed by a markdown table or a bespoke card. */
export function StepHeader({
  step,
  title,
  agent,
  narrative,
}: {
  step?: number;
  title: string;
  agent: boolean;
  narrative: string;
}) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold text-slate-100">
          {step != null ? `${step} · ${title}` : title}
        </span>
        {agent ? (
          <span className="flex items-center gap-1 rounded border border-brand-400/40 bg-brand-500/15 px-1.5 py-0.5 font-mono text-[11px] font-medium text-brand-300">
            <AgentGlyph size={10} />
            Agent
          </span>
        ) : (
          <span className="rounded border border-line-bold bg-wash-strong px-1.5 py-0.5 font-mono text-[11px] font-medium text-slate-300">
            Deterministic
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-slate-300">{narrative}</p>
    </div>
  );
}
