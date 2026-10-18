import { useEffect, useState } from "react";
import { usePipelineStore } from "../../store/usePipelineStore";
import { formatElapsed } from "../../lib/duration";
import { ClockGlyph } from "../common/ClockGlyph";

/** Live "how long has this run taken" pill — always visible (starts at
 * "0.0s" before any run, mirroring the Session pill's $0.0000 start), ticks
 * once/sec while the run is in flight (including HITL wait time), and
 * freezes at the final value once the run completes/errors. */
export function ElapsedTime() {
  const runStartedAt = usePipelineStore((s) => s.runStartedAt);
  const runEndedAt = usePipelineStore((s) => s.runEndedAt);
  const status = usePipelineStore((s) => s.status);
  const live = status === "streaming" || status === "awaiting_input";

  const [, forceTick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [live]);

  const end = runEndedAt ?? Date.now();
  const elapsedMs = runStartedAt == null ? 0 : Math.max(0, end - runStartedAt);

  return (
    <div className="flex items-center gap-2 rounded-full border border-line-strong bg-wash px-3 py-1.5">
      <span className="relative flex h-4 w-4 shrink-0 items-center justify-center">
        <ClockGlyph size={14} animate={live} />
      </span>
      <span className="text-[11px] uppercase tracking-wider text-slate-500">Elapsed</span>
      <span className="text-xs font-medium tabular-nums text-slate-300">{formatElapsed(elapsedMs)}</span>
    </div>
  );
}
