// Labels for AP policy values, read from the policy snapshot the backend stamps
// into session state (app/policy.py Policy.audit(), sourced from Firestore
// policies/ap_rules) so on-screen text can never drift from the rule that ran.
// The fallbacks below only apply before a snapshot has arrived.

import type { FieldValue, InvoiceRecord, PolicySnapshot } from "../api/types";

/** Mirrors the in-code default of REQUIRED_FIELDS in app/config.py. */
export const DEFAULT_REQUIRED_FIELDS = ["vendor", "invoice_no", "po_ref", "total"];
/** Mirrors SCREENING_MIN_STRONG_SIGNALS in app/config.py. */
export const DEFAULT_MIN_STRONG_SIGNALS = 2;

/** "3 of 4" source: how many of the policy's required fields are grounded. */
export function requiredFieldsGrounded(
  rec: InvoiceRecord | undefined,
  snapshot: PolicySnapshot | undefined,
): { grounded: number; total: number } {
  const required = snapshot?.required_fields?.length ? snapshot.required_fields : DEFAULT_REQUIRED_FIELDS;
  const fields = (rec ?? {}) as Record<string, FieldValue | undefined>;
  const grounded = required.filter((name) => fields[name]?.source_grounding === "grounded").length;
  return { grounded, total: required.length };
}

/** Minimum number of independently "strong" screening signals to proceed. */
export function minStrongSignals(snapshot: PolicySnapshot | undefined): number {
  return snapshot?.screening_min_strong_signals ?? DEFAULT_MIN_STRONG_SIGNALS;
}
