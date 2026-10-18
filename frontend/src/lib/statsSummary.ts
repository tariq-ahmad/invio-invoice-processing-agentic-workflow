/** The shape GET /stats returns (app/run_stats.py: get_run_stats). */
export interface RunStatsResponse {
  invoices: number;
  amount_total_usd: number;
  touchless_count: number;
  exception_count: number;
  decision_ms_total: number;
  decision_ms_count: number;
  cost_total_usd: number;
  cost_count: number;
  by_status: Record<string, number>;
  first_recorded_at: string | null;
  last_recorded_at: string | null;
  /** The window requested (days), or null for all recorded runs. */
  window_days: number | null;
  /** Older runs exist beyond the newest ones that were read. */
  truncated: boolean;
  source: string;
}

export interface StatsSummary {
  invoices: number;
  autoPosted: number;
  sentToReview: number;
  /** 0–1, or null when nothing has been processed. */
  autoRate: number | null;
  avgDecisionMs: number | null;
  avgCostUsd: number | null;
  amountTotal: number;
}

export interface OutcomeBreakdown {
  autoPosted: number;
  postedAfterReview: number;
  /** Rejection sent to the vendor. */
  vendorDeclined: number;
  /** Stopped as a duplicate (escalated to ServiceNow, never posted). */
  duplicates: number;
  /** Quarantined by Model Armor. */
  securityFlagged: number;
  /** Vendor-declined + duplicates + security-flagged: everything stopped without posting. */
  rejectedTotal: number;
}

/** Splits `by_status` into the buckets the drawer shows. Legacy posted_sap/posted_coupa
 * statuses count as posted. "escalated" is only ever set by the duplicate alert. */
export function outcomeBreakdown(byStatus: Record<string, number>, autoPosted: number): OutcomeBreakdown {
  const posted = (byStatus.posted_erp ?? 0) + (byStatus.posted_sap ?? 0) + (byStatus.posted_coupa ?? 0);
  const vendorDeclined = byStatus.rejected ?? 0;
  const duplicates = byStatus.escalated ?? 0;
  const securityFlagged = byStatus.quarantined ?? 0;
  return {
    autoPosted,
    postedAfterReview: Math.max(0, posted - autoPosted),
    vendorDeclined,
    duplicates,
    securityFlagged,
    rejectedTotal: vendorDeclined + duplicates + securityFlagged,
  };
}

/** Pure derivations for the stats drawer, kept separate so they can be
 * unit-tested without a browser. Averages cover only the runs that carried the
 * data (a run with no model time or cost doesn't drag the average to zero). */
export function summarizeStats(s: Pick<
  RunStatsResponse,
  | "invoices"
  | "amount_total_usd"
  | "touchless_count"
  | "exception_count"
  | "decision_ms_total"
  | "decision_ms_count"
  | "cost_total_usd"
  | "cost_count"
>): StatsSummary {
  return {
    invoices: s.invoices,
    autoPosted: s.touchless_count,
    sentToReview: s.exception_count,
    autoRate: s.invoices > 0 ? s.touchless_count / s.invoices : null,
    avgDecisionMs: s.decision_ms_count > 0 ? s.decision_ms_total / s.decision_ms_count : null,
    avgCostUsd: s.cost_count > 0 ? s.cost_total_usd / s.cost_count : null,
    amountTotal: s.amount_total_usd,
  };
}
