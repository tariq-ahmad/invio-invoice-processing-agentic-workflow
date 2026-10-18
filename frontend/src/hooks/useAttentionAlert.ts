import { useEffect, useRef } from "react";
import { usePipelineStore } from "../store/usePipelineStore";
import { useAlertPrefs } from "../store/useAlertPrefs";
import { replayHooks } from "../replay/hooks";
import { DOT, setFavicon } from "../lib/favicon";

type Kind = "approval" | "email" | "error" | "done";

const TITLE: Record<Kind, string> = {
  approval: "(1) Approval needed",
  email: "(1) Review the email",
  error: "⚠ Run failed",
  done: "✓ Done",
};
const DOT_FOR: Record<Kind, string> = {
  approval: DOT.attention,
  email: DOT.attention,
  error: DOT.error,
  done: DOT.done,
};

/**
 * When the tab is in the background, surface what needs the user: a title badge
 * and a corner dot on the favicon (plus an optional desktop notification when
 * approval is needed). Everything is restored the moment the tab is visible
 * again or the run moves on. Never fires during a replay.
 */
export function useAttentionAlert(): void {
  const status = usePipelineStore((s) => s.status);
  const interruptKind = usePipelineStore((s) => s.currentInterrupt?.kind);
  const baseTitle = useRef(typeof document !== "undefined" ? document.title : "");
  const prevStatus = useRef(status);

  // Always show the app icon (the page ships without one).
  useEffect(() => {
    setFavicon();
  }, []);

  useEffect(() => {
    const kind: Kind | null =
      status === "awaiting_input" ? (interruptKind === "email_review" ? "email" : "approval")
      : status === "error" ? "error"
      : status === "complete" ? "done"
      : null;

    const restore = () => {
      document.title = baseTitle.current;
      setFavicon();
    };
    const apply = () => {
      if (kind && document.hidden && !replayHooks.active) {
        document.title = `${TITLE[kind]} · ${baseTitle.current}`;
        setFavicon(DOT_FOR[kind]);
      } else {
        restore();
      }
    };

    // A desktop notification, once per transition into "needs you".
    if (
      status === "awaiting_input" &&
      prevStatus.current !== "awaiting_input" &&
      document.hidden &&
      !replayHooks.active &&
      useAlertPrefs.getState().notify &&
      typeof Notification !== "undefined" &&
      Notification.permission === "granted"
    ) {
      const rec = usePipelineStore.getState().state.invoice_record;
      const bits = [rec?.vendor?.value, rec?.invoice_no?.value, rec?.total?.value].filter(Boolean);
      try {
        const n = new Notification(TITLE[kind ?? "approval"].replace("(1) ", ""), {
          body: bits.length ? bits.join(" · ") : "An invoice is waiting for your decision.",
          tag: "invoice-agent-attention",
        });
        n.onclick = () => {
          window.focus();
          n.close();
        };
      } catch {
        /* notifications can throw on some platforms; the title/favicon still work */
      }
    }
    prevStatus.current = status;

    apply();
    document.addEventListener("visibilitychange", apply);
    return () => {
      document.removeEventListener("visibilitychange", apply);
      restore();
    };
  }, [status, interruptKind]);
}
