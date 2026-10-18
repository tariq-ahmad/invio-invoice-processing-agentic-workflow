/**
 * DEV-ONLY preview of the human-approval workbench (A2uiHost -> ApprovalReview)
 * with seeded pipeline state — so the UI can be viewed and exercised without a
 * live AgentMail/Vertex run. Served by `npm run dev` at /approval-preview.html;
 * not part of the production build (vite build only bundles index.html).
 *
 *   /approval-preview.html                      single trigger (over the limit)
 *   /approval-preview.html?scenario=integrity   several findings, ranked
 *   /approval-preview.html?nosource=1           original attachment unavailable
 *   /approval-preview.html?theme=light          light theme
 *   /approval-preview.html?drive=approve|reject|submit|submit-nonote|evidence|evidence-modal|cost
 *                                               scripts the clicks (for screenshots)
 *
 * Fixtures were generated from the real backend (app/nodes/validation.py
 * build_rubric). The "Original document" pane loads /invoices/<id>/pdf, which
 * Vite proxies to the backend — point that at any PDF to see it embedded.
 * Submitting does NOT hit the backend: the structured payload is captured and
 * shown below the card.
 */
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { MotionGlobalConfig } from "framer-motion";
import "../index.css";
import { A2uiHost } from "./../a2ui/A2uiHost";
import { usePipelineStore } from "../store/usePipelineStore";
import { useThemeStore } from "../store/useThemeStore";
import type { PipelineState } from "../api/types";
import fixtures from "./approvalFixtures.json";

const params = new URLSearchParams(window.location.search);
// ?static=1 settles every animation instantly (headless screenshots stall on them).
if (params.get("static")) MotionGlobalConfig.skipAnimations = true;
const scenario = params.get("scenario") === "integrity" ? "integrity" : "over_limit";
const fixture = fixtures[scenario] as unknown as PipelineState;

const seeded: PipelineState = {
  ...fixture,
  ...(params.get("nosource") ? {} : { invoice_message_id: "demo-message" }),
  review_summary: {
    source: "llm",
    model: "gemini-3.5-flash-lite",
    latency_ms: 4107,
    input_tokens: 986,
    output_tokens: 92,
    thinking_tokens: 48,
    text: [
      "**Meridian Steel & Alloys** billed **USD 12,500.00** on invoice **INV-2026-0303** against PO **PO-2026-1010**. Held because the total is about 3x the **USD 4,000 auto-approval limit**.",
      "Check the total and PO number against the original document.",
    ].join("\n\n"),
  },
  memory_summary: {
    source: "llm",
    model: "gemini-3.5-flash-lite",
    latency_ms: 2380,
    input_tokens: 412,
    output_tokens: 68,
    thinking_tokens: 31,
    text: "**Meridian Steel & Alloys** invoices over the limit were approved **2 of 2** times before (**USD 9,800.00** and **USD 11,200.00**). Agent Memory advises **approve**, which is advisory only.",
  },
  screening_model: "gemini-3.5-flash-lite",
  screening_input_tokens: 2140,
  screening_output_tokens: 118,
  screening_thinking_tokens: 40,
  extraction_input_tokens: 5230,
  extraction_output_tokens: 940,
  extraction_thinking_tokens: 612,
  extraction_cache_read_tokens: 1800,
  extraction_model: "gemini-3.8-flash",
  extraction_thought_text:
    "Header block: vendor and invoice number are printed in the top-right.\n" +
    "Totals box: 'Total Due' is the last figure on the page; line items sum to it.\n" +
    "PO reference printed under 'Customer PO'.",
  decision_precedents: {
    vendor: "Meridian Steel & Alloys",
    flag_kinds: ["amount_over_limit"],
    total_count: 2,
    approve_count: 2,
    reject_count: 0,
    recommendation: "approve",
    approved_amounts: ["USD 11200.00", "USD 9800.00"],
    rejected_amounts: [],
    narrative: "You approved the last 2 invoices from Meridian Steel & Alloys that tripped this same check.",
  },
};

let captured: unknown = null;

usePipelineStore.setState({
  status: "awaiting_input",
  state: seeded,
  stepNumbers: { approval: 4 },
  currentInterrupt: { interruptId: "approval_decision", kind: "approval" },
  items: [{ kind: "surface", id: "surface-1", surfaceId: "approval_gate" }],
  // Stand-in for the real resume: capture the structured payload, then "finish".
  submitResume: async (_id: string, value: unknown) => {
    captured = value;
    usePipelineStore.setState({ status: "streaming", currentInterrupt: null });
    window.dispatchEvent(new Event("preview-captured"));
    await new Promise((r) => setTimeout(r, 1400));
    usePipelineStore.setState({ status: "complete" });
  },
});

/** Scripted clicks so the decision flow can be screenshotted headlessly.
 * drive=evidence opens the evidence section and the expanded window. */
async function drive(mode: string) {
  const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
  const click = (sel: string, text?: string) => {
    const el = [...document.querySelectorAll<HTMLElement>(sel)].find((e) => !text || e.textContent?.includes(text));
    el?.click();
  };
  await wait(600);
  if (mode === "cost") {
    click("button[aria-expanded]", "Cost projection");
    return;
  }
  if (mode.startsWith("evidence")) {
    click("button[aria-expanded]", "Evidence");
    click("button[aria-expanded]", "Reasoning");
    await wait(300);
    if (mode === "evidence-modal") click("button", "Expand");
    return;
  }
  click("button[aria-pressed]", mode === "reject" ? "Reject" : "Approve");
  await wait(600);
  click('[role="radiogroup"] button[role="radio"]'); // first reason
  const ta = mode.includes("nonote") ? null : document.querySelector("textarea");
  if (ta) {
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    set.call(ta, "Spoke to the requisitioner — services were delivered in full.");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }
  await wait(400);
  if (mode.startsWith("submit")) click("button", "Confirm");
}

function Preview() {
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const [, force] = useState(0);
  useEffect(() => {
    const on = () => force((n) => n + 1);
    window.addEventListener("preview-captured", on);
    return () => window.removeEventListener("preview-captured", on);
  }, []);
  return (
    <div className="mx-auto max-w-4xl space-y-4 p-5">
      <div className="flex items-center justify-between text-xs text-slate-400">
        <span>
          Dev preview · scenario <b className="text-slate-200">{scenario}</b> ·{" "}
          <a className="underline" href="?scenario=over_limit">over-limit</a> ·{" "}
          <a className="underline" href="?scenario=integrity">integrity</a> ·{" "}
          <a className="underline" href="?nosource=1">no source</a>
        </span>
        <button onClick={toggleTheme} className="rounded border border-line-bold px-2 py-1">
          toggle theme
        </button>
      </div>
      <A2uiHost surfaceId="approval_gate" />
      {captured != null && (
        <div className="rounded-xl border border-line-strong bg-wash p-3">
          <p className="mb-1 text-[11px] uppercase tracking-wider text-slate-500">
            Payload sent to the backend (resume value)
          </p>
          <pre className="overflow-auto font-mono text-[11px] text-slate-300">{JSON.stringify(captured, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

if (params.get("theme") === "light") {
  document.documentElement.dataset.theme = "light";
  useThemeStore.setState({ theme: "light" });
}

const driveMode = params.get("drive");
if (driveMode) void drive(driveMode);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
