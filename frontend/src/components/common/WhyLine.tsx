import { usePipelineStore } from "../../store/usePipelineStore";
import { decisionWhy, type WhyTone } from "../../lib/why";
import { cn } from "../../lib/utils";

const TONE: Record<WhyTone, { box: string; icon: string; glyph: string }> = {
  ok: { box: "border-done-500/30 bg-done-500/[0.07]", icon: "text-done-400", glyph: "M5 13l4 4L19 7" },
  review: { box: "border-amber-400/30 bg-amber-400/[0.07]", icon: "text-amber-300", glyph: "M12 8v5m0 3.5v.01" },
  stop: { box: "border-rose-400/30 bg-rose-500/[0.07]", icon: "text-rose-300", glyph: "M7 7l10 10M17 7L7 17" },
  info: { box: "border-line-strong bg-wash", icon: "text-slate-400", glyph: "M12 11v5m0-8.5v.01" },
};

/** One plain sentence naming what decided this invoice's route or outcome. */
export function WhyLine({ className }: { className?: string }) {
  const state = usePipelineStore((s) => s.state);
  const why = decisionWhy(state);
  if (!why) return null;
  const t = TONE[why.tone];
  return (
    <div className={cn("flex items-start gap-2.5 rounded-lg border px-3 py-2", t.box, className)}>
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={cn("mt-0.5 shrink-0", t.icon)}
        aria-hidden
      >
        <path d={t.glyph} />
      </svg>
      <p className="min-w-0 text-sm leading-snug text-slate-200">
        <span className="font-semibold text-slate-50">Why: </span>
        {why.text}
      </p>
    </div>
  );
}
