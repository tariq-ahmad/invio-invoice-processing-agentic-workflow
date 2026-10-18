import { usePipelineStore } from "../../store/usePipelineStore";
import { deriveRunCost } from "../../lib/runCost";
import { decisionWhy } from "../../lib/why";
import { runStatusChip } from "../../lib/runStatus";
import { formatElapsed } from "../../lib/duration";

const fv = (f?: { value?: string | null }) => (f?.value && String(f.value).trim() ? String(f.value).trim() : "—");

/** Report header that exists only on paper: identifies the run, its outcome and
 * why, above the step cards. Invisible on screen. */
export function PrintHeader() {
  const state = usePipelineStore((s) => s.state);
  const status = usePipelineStore((s) => s.status);
  const currentStage = usePipelineStore((s) => s.currentStage);
  const startedAt = usePipelineStore((s) => s.runStartedAt);
  const endedAt = usePipelineStore((s) => s.runEndedAt);

  const rec = state.invoice_record;
  const chip = runStatusChip({ status, state, currentStage, interrupt: null });
  const why = decisionWhy(state);
  const cost = deriveRunCost(state).totalCost;
  const rows: [string, string][] = [
    ["Vendor", fv(rec?.vendor)],
    ["Invoice", fv(rec?.invoice_no)],
    ["Amount", rec?.total?.value ? `${fv(rec?.currency) === "—" ? "" : fv(rec?.currency)} ${fv(rec?.total)}`.trim() : "—"],
    ["Outcome", chip?.label ?? "—"],
    ["Elapsed", startedAt != null ? formatElapsed((endedAt ?? Date.now()) - startedAt) : "—"],
    ["Model cost", cost > 0 ? `$${cost.toFixed(4)}` : "—"],
  ];

  return (
    <section className="hidden print:block" aria-hidden>
      <h1 className="text-xl font-bold text-slate-50">Invoice processing report</h1>
      <p className="mt-0.5 text-xs text-slate-400">Generated {new Date().toLocaleString()}</p>
      <dl className="mt-3 grid grid-cols-3 gap-x-6 gap-y-2 border-y border-line py-3">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">{k}</dt>
            <dd className="text-sm font-medium text-slate-100">{v}</dd>
          </div>
        ))}
      </dl>
      {why && <p className="mt-3 text-sm text-slate-200"><span className="font-semibold">Why: </span>{why.text}</p>}
    </section>
  );
}
