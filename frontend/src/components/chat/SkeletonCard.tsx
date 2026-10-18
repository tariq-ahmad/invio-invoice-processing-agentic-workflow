import { usePipelineStore } from "../../store/usePipelineStore";
import { STAGES, type StageId } from "../../store/stageMap";
import { AgentGlyph } from "../common/AgentGlyph";
import { useWorkingLabel } from "../../hooks/useWorkingLabel";

/** Stages whose output arrives as a card — while one is running and its card
 * hasn't appeared yet, the feed shows a placeholder of the same shape. */
export const SKELETON_EXCLUDED: ReadonlySet<StageId> = new Set<StageId>(["intake", "armor_intake", "complete"]);

function Bar({ w, h = "h-3" }: { w: string; h?: string }) {
  return <span className={`skel block rounded-md ${h} ${w}`} />;
}

/** Placeholder card for the step that is running right now. */
export function SkeletonCard({ stage }: { stage: StageId }) {
  const def = STAGES[stage];
  const step = usePipelineStore((s) => s.stepNumbers[stage]);
  const label = useWorkingLabel(stage);

  return (
    <div className="card shrink-0 overflow-hidden rounded-2xl" role="status" aria-live="polite" aria-label={label}>
      <div className="h-[3px] w-full bg-gradient-to-r from-brand-400/60 to-brand-600/30" />
      <div className="border-b border-line px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-100">{step != null ? `${step} · ${def.label}` : def.label}</span>
          {def.agent && (
            <span className="flex items-center gap-1 rounded border border-brand-400/40 bg-brand-500/15 px-1.5 py-0.5 font-mono text-[11px] font-medium text-brand-300">
              <AgentGlyph size={10} />
              Agent
            </span>
          )}
        </div>
        <p className="mt-1 text-sm text-slate-400">{label}</p>
      </div>
      <div className="space-y-3 p-4" aria-hidden>
        <div className="grid grid-cols-3 gap-3">
          <Bar w="w-full" h="h-9" />
          <Bar w="w-full" h="h-9" />
          <Bar w="w-full" h="h-9" />
        </div>
        <Bar w="w-5/6" />
        <Bar w="w-2/3" />
        <Bar w="w-3/4" />
      </div>
    </div>
  );
}
