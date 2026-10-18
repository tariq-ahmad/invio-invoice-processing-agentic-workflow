/**
 * DEV-ONLY preview of the Model Armor steps — the pipeline rail rows and the
 * conversation cards — with seeded pipeline state, so every outcome can be viewed
 * without a live AgentMail/Vertex/Model Armor run. Served by `npm run dev` at
 * /armor-preview.html; not part of the production build (vite build only bundles
 * index.html).
 *
 *   /armor-preview.html?scenario=scanning   intake scan in flight
 *   /armor-preview.html?scenario=passed     scan passed, run moves on to screening
 *   /armor-preview.html?scenario=caught     prompt injection caught at intake
 *   /armor-preview.html?scenario=held       scan unavailable, email held (fail-closed)
 *   /armor-preview.html?static=1            settle every animation instantly
 *   /armor-preview.html?theme=light         light theme
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { MotionGlobalConfig } from "framer-motion";
import "../index.css";
import { PipelineTimeline } from "../components/timeline/PipelineTimeline";
import { ConversationPane } from "../components/chat/ConversationPane";
import { usePipelineStore, type ConversationItem, type StageStatus } from "../store/usePipelineStore";
import { useThemeStore } from "../store/useThemeStore";
import type { StageId } from "../store/stageMap";
import type { ArmorVerdict, PipelineState } from "../api/types";

const params = new URLSearchParams(window.location.search);
if (params.get("static")) MotionGlobalConfig.skipAnimations = true;
if (params.get("theme") === "light") {
  document.documentElement.dataset.theme = "light";
  useThemeStore.setState({ theme: "light" });
}

const verdict = (over: Partial<ArmorVerdict>): ArmorVerdict => ({
  scope: "intake",
  direction: "inbound",
  scanned: true,
  matched: false,
  blocked: false,
  filters: [],
  ignored_filters: [],
  checked: ["pi_and_jailbreak", "malicious_uri", "rai", "virus_scan"],
  masked_info_types: [],
  error: null,
  notes: [],
  inputs: [
    { label: "Email header and subject", kind: "text", status: "clean", matched: [] },
    { label: "Email body", kind: "text", status: "clean", matched: [] },
    { label: "PDF attachment", kind: "attachment", status: "clean", detail: "PDF · 142 KB · BluePeak_Invoice_INV-2026-0912.pdf", matched: [] },
  ],
  latency_ms: 412,
  enforce: true,
  template: "invoice_processing_model_armor",
  ...over,
});

type Scenario = {
  done: StageId[];
  active?: StageId;
  steps: ConversationItem[];
  state: PipelineState;
};

const step = (s: string): ConversationItem => ({ kind: "step", id: `step-${s}`, step: s as never });

const SCENARIOS: Record<string, Scenario> = {
  scanning: {
    done: ["intake"],
    active: "armor_intake",
    steps: [step("armor_intake")],
    state: { armor_intake: verdict({ masked_info_types: ["EMAIL_ADDRESS", "PHONE_NUMBER"] }) },
  },
  passed: {
    done: ["intake", "armor_intake"],
    active: "screening",
    steps: [step("armor_intake")],
    state: {
      armor_intake: verdict({
        masked_info_types: ["EMAIL_ADDRESS", "PHONE_NUMBER"],
      }),
    },
  },
  caught: {
    done: ["intake", "armor_intake", "complete"],
    steps: [step("armor_intake")],
    state: {
      pipeline_status: "quarantined",
      armor_intake: verdict({
        matched: true,
        blocked: true,
        filters: ["pi_and_jailbreak", "malicious_uri"],
        ignored_filters: ["sdp"],
        latency_ms: 531,
        inputs: [
          { label: "Email header and subject", kind: "text", status: "clean", matched: [] },
          { label: "Email body", kind: "text", status: "flagged", matched: ["pi_and_jailbreak", "malicious_uri", "sdp"] },
          {
            label: "PDF attachment",
            kind: "attachment",
            status: "flagged",
            detail: "PDF · 142 KB · BluePeak_Invoice_INV-2026-0912.pdf",
            matched: ["pi_and_jailbreak"],
          },
        ],
        findings: [
          { filter: "pi_and_jailbreak", confidence: "high", source: "PDF attachment" },
          { filter: "pi_and_jailbreak", confidence: "medium", source: "Email body" },
          { filter: "malicious_uri", uris: ["http://pay-invoice.example.test/login?id=8841"], source: "Email body" },
          {
            filter: "sdp",
            ignored: true,
            source: "Email body",
            info_types: [
              { name: "BANK_ACCOUNT_NUMBER", likelihood: "likely" },
              { name: "EMAIL_ADDRESS", likelihood: "very_likely" },
            ],
          },
        ],
      }),
      armor_quarantine: {
        reason: "Model Armor flagged: pi_and_jailbreak",
        kind: "threat",
        stage: "intake",
        filters: ["pi_and_jailbreak"],
      },
    },
  },
  held: {
    done: ["intake", "armor_intake", "complete"],
    steps: [step("armor_intake")],
    state: {
      pipeline_status: "error",
      armor_intake: verdict({
        scanned: false,
        error: "DeadlineExceeded: Model Armor timed out",
        blocked: true,
        checked: [],
        inputs: [
          { label: "Email header and subject", kind: "text", status: "error", note: "DeadlineExceeded: Model Armor timed out", matched: [] },
          { label: "Email body", kind: "text", status: "error", note: "DeadlineExceeded: Model Armor timed out", matched: [] },
          { label: "Image attachment", kind: "attachment", status: "not_scanned", detail: "Image · 2.4 MB · scan.tiff", note: "attachment not scanned: unsupported type image/tiff", matched: [] },
        ],
        latency_ms: 15003,
      }),
    },
  },
};

const sc = SCENARIOS[params.get("scenario") ?? "passed"] ?? SCENARIOS.passed;
const stageStatus: Partial<Record<StageId, StageStatus>> = {};
for (const id of sc.done) stageStatus[id] = "done";
if (sc.active) stageStatus[sc.active] = "active";

const stepNumbers: Partial<Record<StageId, number>> = {};
sc.steps.forEach((it, i) => {
  if (it.kind === "step") stepNumbers[it.step as StageId] = i + 2;
});

usePipelineStore.setState({
  status: sc.active && sc.active.startsWith("armor") ? "streaming" : "complete",
  items: sc.steps,
  stageStatus,
  stepNumbers,
  currentStage: sc.active ?? sc.done[sc.done.length - 1] ?? null,
  stageActivatedAt: sc.active ? { [sc.active]: Date.now() } : {},
  state: sc.state,
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <div className="flex h-screen gap-4 p-5">
      <aside className="w-[40%] overflow-y-auto">
        <PipelineTimeline />
      </aside>
      <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-line bg-ink-900/40 p-4">
        <ConversationPane />
      </section>
    </div>
  </StrictMode>,
);
