import { useEffect, useRef, useState } from "react";
import { usePipelineStore } from "../../store/usePipelineStore";
import { useRecorder, selectHasRecording } from "../../replay/recorder";
import { loadRecordingFile, startReplay, useReplay } from "../../replay/controller";
import { buildAuditExport, downloadJson, exportFilename } from "../../lib/auditExport";
import { toast } from "../../store/useToasts";
import { cn } from "../../lib/utils";

function MenuItem({
  label,
  hint,
  disabled,
  onClick,
}: {
  label: string;
  hint?: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={onClick}
      className="flex w-full flex-col items-start rounded-lg px-3 py-2 text-left transition hover:bg-wash-strong disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
    >
      <span className="text-sm font-medium text-slate-100">{label}</span>
      {hint && <span className="text-[11px] leading-snug text-slate-500">{hint}</span>}
    </button>
  );
}

/** Export the finished run (JSON / print-to-PDF) and record/replay entry points. */
export function ExportMenu() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const hasRun = usePipelineStore((s) => !!s.state.invoice_record || !!s.state.pipeline_status);
  const hasRecording = useRecorder(selectHasRecording);
  const replaying = useReplay((s) => s.active);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const run = (fn: () => void) => () => {
    setOpen(false);
    fn();
  };

  const downloadAudit = () => {
    const s = usePipelineStore.getState();
    downloadJson(
      exportFilename("invoice-audit", s.state),
      buildAuditExport({
        state: s.state,
        status: s.status,
        stageStatus: s.stageStatus,
        runStartedAt: s.runStartedAt,
        runEndedAt: s.runEndedAt,
      }),
    );
    toast("Audit record downloaded", "success");
  };

  const recordingSnapshot = () => {
    const st = usePipelineStore.getState().state;
    return useRecorder.getState().snapshot({
      vendor: st.invoice_record?.vendor?.value ?? undefined,
      invoiceNo: st.invoice_record?.invoice_no?.value ?? undefined,
      total: st.invoice_record?.total?.value ?? undefined,
      outcome: st.pipeline_status,
    });
  };

  const downloadRecording = () => {
    const rec = recordingSnapshot();
    if (!rec) return;
    downloadJson(exportFilename("invoice-recording", usePipelineStore.getState().state), rec);
    toast("Recording downloaded — load it later to replay this run", "success");
  };

  const replayLast = () => {
    const rec = recordingSnapshot();
    if (rec) startReplay(rec);
  };

  return (
    <div ref={wrapRef} className="no-print relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-xl border border-line-strong bg-wash px-2.5 py-1.5 text-xs font-medium text-slate-300 transition hover:border-line-bold hover:bg-wash-strong hover:text-slate-100"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14" />
        </svg>
        Export
      </button>

      {open && (
        <div
          role="menu"
          className={cn(
            "card absolute right-0 top-full z-30 mt-2 w-72 rounded-xl p-1.5 shadow-[var(--shadow-raised)]",
          )}
        >
          <p className="px-3 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500">This run</p>
          <MenuItem
            label="Download audit record"
            hint="JSON: invoice, decision, checks, cost, full state"
            disabled={!hasRun}
            onClick={run(downloadAudit)}
          />
          <MenuItem
            label="Print / Save as PDF"
            hint="Opens your browser's print dialog with a clean report layout"
            disabled={!hasRun}
            onClick={run(() => window.print())}
          />
          <div className="my-1.5 border-t border-line" />
          <p className="px-3 pb-1 pt-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Replay</p>
          <MenuItem
            label="Replay last run"
            hint="Play the recorded run back — no live backend needed"
            disabled={!hasRecording || replaying}
            onClick={run(replayLast)}
          />
          <MenuItem
            label="Download recording"
            hint="Contains the invoice data shown in the run"
            disabled={!hasRecording}
            onClick={run(downloadRecording)}
          />
          <MenuItem label="Load a recording…" hint="Pick a recording file to replay" onClick={run(() => fileRef.current?.click())} />
        </div>
      )}

      <input
        ref={fileRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = "";
          if (f) void loadRecordingFile(f);
        }}
      />
    </div>
  );
}
