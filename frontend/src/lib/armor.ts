/**
 * Model Armor presentation logic, shared by the pipeline rail (PipelineTimeline)
 * and the conversation card (ArmorCard): which verdict belongs to which scan
 * point, how a verdict reads (passed / caught / ...), and the copy for each.
 *
 * The verdicts themselves come from the backend (app/model_armor.py
 * ArmorVerdict.to_state()); nothing here decides whether content is safe.
 */
import type { ArmorFinding, ArmorVerdict, PipelineState } from "../api/types";
import type { ArmorStageId } from "../store/stageMap";
import { formatLatency } from "./duration";

export const FILTER_LABELS: Record<string, string> = {
  pi_and_jailbreak: "Prompt injection / jailbreak",
  malicious_uri: "Malicious URL",
  rai: "Harmful content",
  sdp: "Sensitive data",
  csam: "Child safety",
  virus_scan: "Malware",
};

/** One line on what each filter looks for, shown under its name in the checklist. */
export const FILTER_BLURB: Record<string, string> = {
  pi_and_jailbreak: "Hidden instructions that try to take over the AI",
  malicious_uri: "Phishing and malware links",
  rai: "Hateful, harassing, dangerous or sexual content",
  sdp: "Personal or confidential data",
  csam: "Child-safety material",
  virus_scan: "Malware inside the attachment",
};

/** Lower-case names for running text ("Checking prompt injection..."). */
export const FILTER_SHORT: Record<string, string> = {
  pi_and_jailbreak: "prompt injection",
  malicious_uri: "malicious links",
  rai: "harmful content",
  sdp: "sensitive data",
  csam: "child safety",
  virus_scan: "malware",
};

const FILTER_ORDER = ["pi_and_jailbreak", "malicious_uri", "rai", "sdp", "csam", "virus_scan"];

/** Filters listed while a scan is in flight and its verdict isn't known yet; the
 * checklist is replaced by the filters Model Armor actually reports once it is. */
const DEFAULT_FILTERS: Record<ArmorStageId, string[]> = {
  armor_intake: ["pi_and_jailbreak", "malicious_uri", "rai", "virus_scan"],
};

/** Filters this product doesn't use: left out of the checklist unless one actually
 * stopped the content, so a block is never hidden. */
const HIDDEN_FILTERS = new Set(["sdp", "csam"]);

export function filterLabel(key: string): string {
  return FILTER_LABELS[key] ?? key;
}

export type ArmorOutcome =
  | "pass" // scanned, nothing found
  | "caught" // a filter matched and the content was stopped
  | "held" // no verdict (scan failed) and the pipeline fails closed
  | "flagged" // a filter matched but enforcement is off (inspect-only)
  | "unscanned"; // nothing scannable, or the scan failed and the pipeline fails open

/** Visual tone of an outcome: green passed, red stopped, amber needs a look. */
export type ArmorTone = "pass" | "stop" | "warn";

export function armorOutcome(v: ArmorVerdict | undefined): ArmorOutcome | undefined {
  if (!v) return undefined;
  if (v.matched) return v.blocked ? "caught" : "flagged";
  if (v.error) return v.blocked ? "held" : "unscanned";
  if (!v.scanned) return "unscanned";
  return "pass";
}

export function outcomeTone(o: ArmorOutcome | undefined): ArmorTone {
  if (o === "caught" || o === "held") return "stop";
  if (o === "flagged" || o === "unscanned") return "warn";
  return "pass";
}

/** The verdict a scan point reports, straight from pipeline state. */
export function armorVerdict(stage: ArmorStageId, state: PipelineState): ArmorVerdict | undefined {
  switch (stage) {
    case "armor_intake":
      return state.armor_intake;
  }
}

export interface ArmorFilterRow {
  key: string;
  label: string;
  blurb?: string;
  /** Why this filter fired, one per place it was found (empty when clear). */
  findings: ArmorFinding[];
  /** clear = ran, nothing found · detected = matched · allowed = matched but exempt by policy */
  result: "clear" | "detected" | "allowed";
}

/** The checklist for a verdict: every filter that ran, in a stable order. */
export function armorFilterRows(v: ArmorVerdict): ArmorFilterRow[] {
  const keys = [...new Set([...(v.checked ?? []), ...v.filters, ...v.ignored_filters])].filter(
    (k) => !HIDDEN_FILTERS.has(k) || v.filters.includes(k),
  );
  keys.sort((a, b) => rank(a) - rank(b));
  return keys.map((key) => ({
    key,
    label: filterLabel(key),
    blurb: FILTER_BLURB[key],
    findings: (v.findings ?? []).filter((f) => f.filter === key),
    result: v.filters.includes(key) ? "detected" : v.ignored_filters.includes(key) ? "allowed" : "clear",
  }));
}

/** "BANK_ACCOUNT_NUMBER" -> "Bank account number". */
function humanize(name: string): string {
  const s = name.toLowerCase().replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** One line on why a filter fired. `mono` marks text that must read verbatim
 * (a URL); the card renders everything here as inert plain text. */
export function describeFinding(f: ArmorFinding): { what?: string; mono?: boolean; source?: string } {
  const out: { what?: string; mono?: boolean; source?: string } = { source: f.source };
  if (f.filter === "pi_and_jailbreak" && f.confidence) {
    out.what = `${humanize(f.confidence)} confidence`;
  } else if (f.filter === "malicious_uri" && f.uris?.length) {
    out.what = f.uris.join(", ");
    out.mono = true;
  } else if (f.filter === "sdp" && f.info_types?.length) {
    out.what = f.info_types
      .map((t) => (t.likelihood ? `${humanize(t.name)} (${t.likelihood.replace(/_/g, " ")})` : humanize(t.name)))
      .join(", ");
  } else if (f.filter === "rai" && f.categories?.length) {
    out.what = f.categories.map(humanize).join(", ");
  } else if (f.filter === "virus_scan" && f.threats?.length) {
    out.what = f.threats.join(", ");
  }
  return out;
}

/** Filter names for the in-flight animation. */
export function armorScanningFilters(stage: ArmorStageId): { key: string; label: string; blurb?: string }[] {
  return DEFAULT_FILTERS[stage].map((key) => ({ key, label: filterLabel(key), blurb: FILTER_BLURB[key] }));
}

function rank(key: string): number {
  const i = FILTER_ORDER.indexOf(key);
  return i === -1 ? FILTER_ORDER.length : i;
}

export interface ArmorCopy {
  /** Shown while the scan runs. */
  scanning: string;
  /** Shown per outcome once it finished. */
  result: Record<ArmorOutcome, string>;
  /** Short form of the above, for the Verdict section's subtitle. */
  short: Record<ArmorOutcome, string>;
}

export const ARMOR_COPY: Record<ArmorStageId, ArmorCopy> = {
  armor_intake: {
    scanning: "Model Armor is scanning the email and its attachment before any AI agent reads them.",
    result: {
      pass: "No threats found. The email and attachment were passed on to the AI agents.",
      caught:
        "Stopped before any AI agent saw the email. Nothing was extracted, validated or posted, and the email is quarantined for the AP team.",
      held: "Model Armor could not complete the scan, so the email was held instead of being processed unchecked. It will be retried.",
      flagged: "Model Armor flagged this content, but enforcement is off (inspect-only), so processing continues.",
      unscanned: "There was nothing Model Armor could scan here, so processing continues.",
    },
    short: {
      pass: "Passed · cleared for screening",
      caught: "Caught · quarantined",
      held: "Held · scan unavailable",
      flagged: "Flagged · not enforced",
      unscanned: "Not scanned",
    },
  },
};

/** The final word on a caught email: what was found, where, and what happened to the
 * email. Names filters and places only, never the flagged content. */
export function armorCaughtMessage(v: ArmorVerdict): string {
  const what = v.filters.map((f) => FILTER_SHORT[f] ?? filterLabel(f).toLowerCase());
  const found = what.length > 1 ? `${what.slice(0, -1).join(", ")} and ${what[what.length - 1]}` : (what[0] ?? "a security threat");
  const places = [
    ...new Set((v.findings ?? []).filter((f) => !f.ignored && v.filters.includes(f.filter)).map((f) => f.source)),
  ]
    .filter((s): s is string => !!s)
    .map((s) => `the ${s.startsWith("PDF") ? s : s.charAt(0).toLowerCase() + s.slice(1)}`);
  const where = places.length > 1 ? `${places.slice(0, -1).join(", ")} and ${places[places.length - 1]}` : places[0];
  return (
    `The email was quarantined. Model Armor found ${found}${where ? ` in ${where}` : ""}, so no AI agent read it ` +
    "and nothing was extracted, approved or posted. It is labelled AP-Quarantined and left in the inbox " +
    "for the AP team to review. If it is a genuine invoice, ask the vendor to resend a clean copy."
  );
}

/** "Passed" / "Caught" / ... — the short word used on the rail and the card badge. */
export const OUTCOME_WORD: Record<ArmorOutcome, string> = {
  pass: "Passed",
  caught: "Caught",
  held: "Held",
  flagged: "Flagged",
  unscanned: "Not scanned",
};

/** One-word chip on the rail once a scan finished. */
export const OUTCOME_CHIP: Record<ArmorOutcome, string> = {
  pass: "Clear",
  caught: "Blocked",
  held: "Held",
  flagged: "Flagged",
  unscanned: "Skipped",
};

/** Rail sub-line for a finished scan: outcome word plus the one detail that matters. */
export function armorRailSummary(v: ArmorVerdict | undefined): string {
  const o = armorOutcome(v);
  if (!v || !o) return "Done";
  switch (o) {
    case "pass":
      return v.latency_ms != null ? `Passed · ${formatLatency(v.latency_ms)}` : "Passed";
    case "caught":
    case "flagged":
      return `${OUTCOME_WORD[o]} · ${v.filters.map(filterLabel).join(", ")}`;
    case "held":
      return "Held · scan unavailable";
    default:
      return "Nothing to scan";
  }
}
