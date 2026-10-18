// @vitest-environment happy-dom
//
// Mounts the real <App /> in a DOM and drives a replayed run through it, so the
// wiring tsc can't see (hooks, lazy chunks, fold logic, header, why-lines,
// graph navigation) is exercised without a browser.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AdkEvent } from "./api/types";
import { RECORDING_SCHEMA, parseRecording } from "./replay/format";
import { continueReplay, exitReplay, startReplay, useReplay } from "./replay/controller";
import { loadMode, useFeedUi, type FeedMode } from "./store/useFeedUi";
import { useRunStats } from "./store/useRunStats";
import { usePipelineStore } from "./store/usePipelineStore";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// happy-dom has no <canvas>; the success confetti would throw on its first frame.
vi.mock("canvas-confetti", () => ({ default: vi.fn() }));

const AUTHOR = "invoice_processing_workflow";
const ev = (node: string, delta?: Record<string, unknown>, extra: Partial<AdkEvent> = {}): AdkEvent => ({
  author: AUTHOR,
  nodeInfo: { path: `${AUTHOR}@1/${node}@1` },
  invocationId: "inv-1",
  ...(delta ? { actions: { stateDelta: delta } } : {}),
  ...extra,
});

const recording = () =>
  parseRecording({
    schema: RECORDING_SCHEMA,
    recordedAt: "2026-10-15T00:00:00Z",
    segments: [
      {
        kind: "run",
        events: [
          ev("fetch_invoice_email", { invoice_message_id: "m1" }),
          ev("screen_email_signal", { screening_route: "invoice" }),
          ev("route_extraction_strategy", { extraction_strategy: "inline", extraction_recommended_model: "flash" }),
          ev("emit_extraction_card", {
            extraction_model: "gemini-3.8-flash",
            extraction_latency_ms: 1800,
            invoice_record: {
              vendor: { value: "Acme Corp" },
              invoice_no: { value: "INV-1042" },
              total: { value: "12,400.00" },
              currency: { value: "USD" },
            },
          }),
          ev("validate_invoice", {
            validation_route: "human_review",
            validation_flags: ["Over the auto-approval limit"],
            validation_rubric: [
              { id: "within_limit", label: "Limit", headline: "Over the auto-approval limit", criterion: "", observed: "USD 12,400", status: "flag", rank: 1 },
            ],
          }),
          ev("human_approval_gate", undefined, {
            content: { role: "model", parts: [{ functionCall: { name: "adk_request_input", id: "int-1", args: { message: "Approve?" } } }] },
          }),
        ],
      },
      {
        kind: "resume",
        events: [
          ev("post_to_erp", { posting_result: { system: "ERP", document_id: "D-1" } }),
          ev("pipeline_complete", { pipeline_status: "posted_erp" }),
        ],
      },
    ],
  });

// What the (stubbed) GET /stats returns; tests swap it.
const STATS_OK = {
  invoices: 12,
  amount_total_usd: 48250.5,
  touchless_count: 9,
  exception_count: 3,
  decision_ms_total: 24000,
  decision_ms_count: 12,
  cost_total_usd: 0.6,
  cost_count: 12,
  by_status: { posted_erp: 9, rejected: 2, escalated: 1 },
  first_recorded_at: "2026-10-01T09:00:00+00:00",
  last_recorded_at: "2026-10-15T09:00:00+00:00",
  window_days: null,
  truncated: false,
  source: "firestore",
};
let statsReply: () => { status: number; body: unknown } = () => ({ status: 200, body: STATS_OK });
const fetchLog: string[] = [];

let root: Root | null = null;
let host: HTMLElement | null = null;
const settle = { timeout: 8000, interval: 20 };

beforeAll(() => {
  // happy-dom has no layout: give the graph a width so it renders its nodes.
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 600 });
  Element.prototype.scrollIntoView = vi.fn();
  // happy-dom's WAAPI stub rejects on cancel; without it Framer Motion uses its JS animator.
  delete (Element.prototype as { animate?: unknown }).animate;
  vi.stubGlobal(
    "EventSource",
    class {
      onopen: (() => void) | null = null;
      onerror: (() => void) | null = null;
      addEventListener() {}
      close() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      fetchLog.push(String(url));
      if (String(url).startsWith("/stats")) {
        const r = statsReply();
        return { ok: r.status < 400, status: r.status, json: async () => r.body, text: async () => "" };
      }
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    }),
  );
});

afterEach(async () => {
  await act(async () => {
    root?.unmount();
  });
  host?.remove();
  root = null;
  host = null;
  exitReplay();
  useFeedUi.getState().clear();
  useRunStats.setState({ data: null, load: "idle", error: null, fetchedAt: null, range: 0 });
  statsReply = () => ({ status: 200, body: STATS_OK });
  fetchLog.length = 0;
  localStorage.clear();
});

async function mountApp(mode: FeedMode = "auto") {
  useFeedUi.setState({ mode, pin: null });
  const { default: App } = await import("./App");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<App />);
  });
}

const q = (sel: string) => host!.querySelector(sel);
const qa = (sel: string) => [...host!.querySelectorAll(sel)];
const text = () => host!.textContent ?? "";

async function playToDecision() {
  useReplay.setState({ speed: 0 });
  await act(async () => {
    startReplay(recording());
  });
  await vi.waitFor(async () => {
    await act(async () => {});
    expect(useReplay.getState().awaitingContinue).toBe(true);
  }, settle);
}

describe("App with a replayed run", () => {
  it("renders the idle shell with the empty state and a loader for recordings", async () => {
    await mountApp();
    expect(text()).toContain("Monitoring inbox");
    expect(text()).toContain("Or load a recorded run to replay");
    expect(text()).toContain("No invoice yet");
    expect(q('[role="radiogroup"][aria-label="How to show steps"]')).not.toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull(); // stats drawer closed
    // The header is the rounded floating bar, not a flat rectangle.
    const bar = q(".app-feed > div") as HTMLElement;
    expect(bar.className).toContain("glass-float");
    expect(bar.className).toContain("rounded-2xl");
    // The top bar (and its cost popover) must stack above the sticky invoice bar,
    // or the popover is painted underneath it.
    const z = (el: Element) => Number(/(?:^|\s)z-(\d+)(?:\s|$)/.exec(el.className)?.[1] ?? 0);
    expect(z(q("header")!)).toBeGreaterThan(z(bar));
  });

  it("shows every step in full by default (Expand all)", async () => {
    localStorage.clear();
    expect(loadMode()).toBe("all");
    // ...a saved choice still wins, and junk falls back to the default.
    localStorage.setItem("feed:mode", "focus");
    expect(loadMode()).toBe("focus");
    localStorage.setItem("feed:mode", "nonsense");
    expect(loadMode()).toBe("all");
  });

  it("pins the invoice context, folds finished steps and explains the review route", async () => {
    await mountApp();
    await playToDecision();

    // Sticky header: vendor · invoice · amount + status.
    const header = q(".app-feed > div")!;
    expect(header.textContent).toContain("Acme Corp");
    expect(header.textContent).toContain("INV-1042");
    expect(header.textContent).toContain("USD 12,400.00");
    expect(header.textContent).toContain("Needs your approval");

    // Finished steps are folded to one-line summaries; the card they hide stays mounted.
    const folded = qa('[data-feed-item][data-folded]');
    expect(folded.length).toBeGreaterThan(0);
    expect(folded.some((el) => el.textContent?.includes("Acme Corp · INV-1042"))).toBe(true);
    expect(qa("[data-fold-inner][inert]").length).toBe(folded.length);

    // The approval card (waiting on the user) is open, and has no "Why" line: its own
    // briefing already explains the review, so the line only appears on the result card.
    let approval!: Element;
    await vi.waitFor(async () => {
      await act(async () => {});
      approval = q('[data-feed-item][data-stage="approval"]')!;
      expect(approval.textContent).toContain("Review before this is posted");
    }, settle);
    expect(approval.hasAttribute("data-folded")).toBe(false);
    expect(approval.textContent).not.toContain("Why:");
    expect(approval.textContent).not.toContain("Routed to review");

    // Replay chrome is present and the decision controls are read-only.
    expect(q('[aria-label="Replay controls"]')).not.toBeNull();
    expect(text()).toContain("The recorded run paused here for a human decision");
  });

  it("expands a folded step on click, and the fold toggle folds it again", async () => {
    await mountApp();
    await playToDecision();

    const row = qa("[data-fold-row]").find((b) => b.closest("[data-folded]"))! as HTMLButtonElement;
    const item = row.closest("[data-feed-item]") as HTMLElement;
    await act(async () => {
      row.click();
    });
    expect(item.hasAttribute("data-folded")).toBe(false);

    const fold = item.querySelector("[data-fold-toggle]") as HTMLButtonElement;
    await act(async () => {
      fold.click();
    });
    expect(item.hasAttribute("data-folded")).toBe(true);
  });

  it("modes: 'Expand all' unfolds everything, 'Focus' shows only the current step", async () => {
    await mountApp();
    await playToDecision();
    const radio = (label: string) => qa('[aria-label="How to show steps"] [role="radio"]').find((b) => b.textContent === label)! as HTMLButtonElement;

    await act(async () => {
      radio("Expand all").click();
    });
    expect(qa("[data-feed-item][data-folded]").length).toBe(0);

    await act(async () => {
      radio("Focus").click();
    });
    // The other cards leave through their exit animation, then only the current one remains.
    await vi.waitFor(async () => {
      await act(async () => {});
      expect(qa("[data-feed-item]").length).toBe(1);
    }, settle);

    await act(async () => {
      radio("Auto-fold").click();
    });
    expect(qa("[data-feed-item]").length).toBeGreaterThan(1);
  });

  it("jumps from a graph node to its card and expands it", async () => {
    await mountApp();
    await playToDecision();

    const node = qa('[role="button"]').find((e) => e.getAttribute("aria-label") === "Extraction: show its card") as HTMLElement;
    expect(node).toBeTruthy();
    await act(async () => {
      node.click();
    });
    const card = q('[data-feed-item][data-stage="extraction"]')!;
    expect(card.hasAttribute("data-folded")).toBe(false);
    expect(useFeedUi.getState().flash?.id).toBeTruthy();
  });

  it("shows the outcome why-line and result card after the recorded decision plays", async () => {
    await mountApp();
    await playToDecision();
    await act(async () => {
      continueReplay();
    });
    await vi.waitFor(async () => {
      await act(async () => {});
      expect(useReplay.getState().finished).toBe(true);
    }, settle);

    const header = q(".app-feed > div")!;
    expect(header.textContent).toContain("Posted to ERP");
    const result = q("#result-card")!;
    expect(result).not.toBeNull();
    expect(result.textContent).toContain("Why:");
    expect(result.textContent).toContain("Posted to ERP");
    // The printable report header exists (hidden on screen).
    expect(text()).toContain("Invoice processing report");
  });

  describe("stats drawer (live from Firestore)", () => {
    const open = async () => {
      const btn = q('button[aria-label="Insights"]') as HTMLButtonElement;
      await act(async () => {
        btn.click();
      });
    };
    const dialog = () => document.querySelector('[role="dialog"][aria-label="Insights"]') as HTMLElement | null;

    it("fetches /stats when opened and shows the Firestore numbers", async () => {
      await mountApp();
      await open();
      await vi.waitFor(async () => {
        await act(async () => {});
        expect(dialog()!.textContent).not.toContain("Live from Firestore");
        expect(dialog()!.textContent).toContain("12"); // invoices
        expect(dialog()!.textContent).toContain("75%"); // 9 of 12 auto-posted
        expect(dialog()!.textContent).toContain("2.0s"); // 24000ms / 12 runs
        expect(dialog()!.textContent).toContain("$0.0500"); // 0.6 / 12 runs
        expect(dialog()!.textContent).toContain("Straight-Through");
        expect(dialog()!.textContent).toContain("$48,251"); // total value
      }, settle);
      expect(fetchLog).toContain("/stats?days=0");
      expect(dialog()!.textContent).toMatch(/Last updated at/);
    });

    it("re-fetches for the chosen time range", async () => {
      await mountApp();
      await open();
      await vi.waitFor(() => expect(fetchLog).toContain("/stats?days=0"), settle);
      const seven = [...dialog()!.querySelectorAll('[role="radio"]')].find((b) => b.textContent === "7 days") as HTMLElement;
      await act(async () => {
        seven.click();
      });
      await vi.waitFor(() => expect(fetchLog).toContain("/stats?days=7"), settle);
    });

    it("says so when nothing has been recorded", async () => {
      statsReply = () => ({
        status: 200,
        body: { ...STATS_OK, invoices: 0, by_status: {}, first_recorded_at: null, last_recorded_at: null },
      });
      await mountApp();
      await open();
      await vi.waitFor(async () => {
        await act(async () => {});
        expect(dialog()!.textContent).toContain("No runs recorded yet");
      }, settle);
    });

    it("reports a Firestore outage instead of showing zeros, and keeps the last numbers", async () => {
      await mountApp();
      await open();
      await vi.waitFor(async () => {
        await act(async () => {});
        expect(dialog()!.textContent).toContain("75%");
      }, settle);

      statsReply = () => ({ status: 503, body: { detail: "down" } });
      const refresh = [...dialog()!.querySelectorAll("button")].find((b) => b.textContent === "Refresh") as HTMLElement;
      await act(async () => {
        refresh.click();
      });
      await vi.waitFor(async () => {
        await act(async () => {});
        expect(dialog()!.querySelector('[role="alert"]')!.textContent).toContain("Firestore is temporarily unavailable");
      }, settle);
      expect(dialog()!.textContent).toContain("Showing the last numbers we got");
      expect(dialog()!.textContent).toContain("75%");
    });

    it("closes on Escape", async () => {
      await mountApp();
      await open();
      expect(dialog()).not.toBeNull();
      await act(async () => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      });
      await vi.waitFor(() => expect(dialog()).toBeNull(), settle);
    });

    it("has no reset: the numbers are shared, not this browser's", async () => {
      await mountApp();
      await open();
      await vi.waitFor(async () => {
        await act(async () => {});
        expect(dialog()!.textContent).toContain("75%");
      }, settle);
      expect(dialog()!.textContent).not.toContain("Reset");
    });
  });

  it("exiting a replay returns to a clean idle view", async () => {
    await mountApp();
    await playToDecision();
    await act(async () => {
      exitReplay();
    });
    expect(usePipelineStore.getState().items).toHaveLength(0);
    expect(q('[aria-label="Replay controls"]')).toBeNull();
    expect(text()).toContain("Monitoring inbox");
  });
});
