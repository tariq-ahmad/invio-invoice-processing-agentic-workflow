import { useState } from "react";
import { usePipelineStore } from "../../store/usePipelineStore";
import { STAGES } from "../../store/stageMap";
import { SPEEDS, speedLabel } from "../../lib/pacing";
import {
  continueReplay,
  exitReplay,
  restartReplay,
  seekTo,
  setPlaying,
  setSpeed,
  useReplay,
} from "../../replay/controller";
import { cn } from "../../lib/utils";

const Icon = ({ d, label }: { d: string; label: string }) => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-label={label} role="img">
    <path d={d} />
  </svg>
);
const PLAY = "M8 5v14l11-7z";
const PAUSE = "M7 5h4v14H7zM13 5h4v14h-4z";
const RESTART = "M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z";

/** Transport controls for a replayed run: play/pause, speed, scrubber, exit. */
export function ReplayBar() {
  const r = useReplay();
  const currentStage = usePipelineStore((s) => s.currentStage);
  // While the thumb is dragged we show its position; the seek happens on release,
  // so scrubbing doesn't restart playback on every pixel.
  const [draft, setDraft] = useState<number | null>(null);
  if (!r.active) return null;

  const shown = draft ?? r.position;
  const commit = () => {
    if (draft != null) seekTo(draft, r.playing);
    setDraft(null);
  };
  const stageLabel = currentStage ? STAGES[currentStage].label : "Starting";

  return (
    <div
      role="region"
      aria-label="Replay controls"
      className="no-print mt-3 shrink-0 rounded-xl border border-brand-400/30 bg-brand-500/[0.07] p-3"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="rounded-md bg-brand-500/25 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wider text-brand-200">
          Replay
        </span>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={restartReplay}
            title="Restart"
            className="grid h-8 w-8 place-items-center rounded-lg text-slate-300 transition hover:bg-wash-strong hover:text-slate-50"
          >
            <Icon d={RESTART} label="Restart" />
          </button>
          <button
            type="button"
            onClick={() => setPlaying(!r.playing)}
            disabled={r.awaitingContinue}
            title={r.playing ? "Pause" : r.finished ? "Play again" : "Play"}
            className="grid h-8 w-8 place-items-center rounded-lg bg-brand-500/25 text-brand-100 transition hover:bg-brand-500/40 disabled:opacity-40"
          >
            <Icon d={r.playing ? PAUSE : PLAY} label={r.playing ? "Pause" : "Play"} />
          </button>
        </div>

        <div role="radiogroup" aria-label="Playback speed" className="flex rounded-lg border border-line-strong bg-wash p-0.5">
          {SPEEDS.map((sp) => (
            <button
              key={sp}
              type="button"
              role="radio"
              aria-checked={r.speed === sp}
              onClick={() => setSpeed(sp)}
              className={cn(
                "rounded-md px-2 py-0.5 text-xs font-medium tabular-nums transition-colors",
                r.speed === sp ? "bg-brand-500/25 text-brand-100" : "text-slate-400 hover:text-slate-100",
              )}
            >
              {speedLabel(sp)}
            </button>
          ))}
        </div>

        <span className="ml-auto text-xs tabular-nums text-slate-400">
          {r.finished ? "Finished" : stageLabel} · event {shown} of {r.total}
        </span>
        <button
          type="button"
          onClick={exitReplay}
          className="rounded-lg border border-line-strong bg-wash px-2.5 py-1 text-xs font-medium text-slate-300 transition hover:border-line-bold hover:text-slate-50"
        >
          Exit replay
        </button>
      </div>

      <div className="relative mt-2.5">
        <input
          type="range"
          min={0}
          max={r.total}
          step={1}
          value={shown}
          aria-label="Replay position"
          aria-valuetext={`Event ${shown} of ${r.total}`}
          onChange={(e) => setDraft(Number(e.target.value))}
          onPointerUp={commit}
          onKeyUp={commit}
          onBlur={commit}
          className="relative z-10 h-5 w-full cursor-pointer accent-brand-500"
        />
        {/* Where the recorded run stopped for a human decision. */}
        {r.segmentEnds.slice(0, -1).map((end) => (
          <span
            key={end}
            title="Human decision point"
            aria-hidden
            className="pointer-events-none absolute top-0.5 h-4 w-0.5 rounded-full bg-amber-400"
            style={{ left: `calc(${(end / Math.max(1, r.total)) * 100}% - 1px)` }}
          />
        ))}
      </div>

      {r.awaitingContinue && (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-400/30 bg-amber-400/10 px-3 py-2">
          <p className="text-xs text-amber-100">
            The recorded run paused here for a human decision. Continue to play the decision that was made.
          </p>
          <button
            type="button"
            onClick={continueReplay}
            className="rounded-lg bg-gradient-to-r from-brand-500 to-brand-600 px-3 py-1 text-xs font-semibold text-white shadow-sm transition hover:brightness-110"
          >
            Continue
          </button>
        </div>
      )}
    </div>
  );
}
