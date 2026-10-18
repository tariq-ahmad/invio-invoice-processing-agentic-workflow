import { useEffect, useRef } from "react";
import { usePipelineStore } from "../store/usePipelineStore";

/**
 * Two-channel auto-trigger:
 * 1. SSE (GET /events) — instant push when browser tab is open and connected.
 * 2. Polling (GET /events/latest every 5s) — fallback when SSE drops due to
 *    proxy timeouts or server restarts. Deduped by message_id so one email
 *    never triggers the pipeline twice.
 */
// Persisted across refreshes/new-sessions so a replayed event (server keeps the
// last new_invoice for a short TTL) can never re-trigger the same email in a
// loop. A genuinely new email has a different message_id and overwrites this.
const AUTOSTART_KEY = "lastAutoStartedInvoiceId";

export function useInvoiceEvents() {
  const autoStart = usePipelineStore((s) => s.autoStart);
  const autoStartRef = useRef(autoStart);
  autoStartRef.current = autoStart;

  // Per-session fallback guard for the rare case an event carries no message_id.
  const lastTriggeredRef = useRef<string | undefined>(undefined);

  function fire(messageId?: string) {
    if (messageId) {
      // Dedup across refreshes via localStorage — breaks the replay loop.
      let lastProcessed: string | null = null;
      try {
        lastProcessed = localStorage.getItem(AUTOSTART_KEY);
      } catch {
        /* ignore storage errors */
      }
      if (messageId === lastProcessed) return; // already triggered this email
      try {
        localStorage.setItem(AUTOSTART_KEY, messageId);
      } catch {
        /* ignore storage errors */
      }
      lastTriggeredRef.current = messageId;
      autoStartRef.current(messageId);
      return;
    }

    // No message_id (rare) — fall back to in-session dedup only.
    if (lastTriggeredRef.current === undefined) {
      lastTriggeredRef.current = "__no_id__";
      autoStartRef.current(undefined);
    }
  }

  useEffect(() => {
    let es: EventSource | null = null;
    let destroyed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    // ── SSE connection ────────────────────────────────────────────────────────
    function connect() {
      if (destroyed) return;
      es = new EventSource("/events");

      es.addEventListener("new_invoice", (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data) as { message_id?: string };
          fire(data.message_id);
        } catch {
          fire(undefined);
        }
      });

      es.onerror = () => {
        es?.close();
        if (!destroyed) {
          retryTimer = setTimeout(connect, 3000);
        }
      };
    }

    // ── Polling fallback ──────────────────────────────────────────────────────
    async function poll() {
      if (destroyed) return;
      try {
        const res = await fetch("/events/latest", { cache: "no-store" });
        if (res.status === 200) {
          const data = (await res.json()) as { type?: string; data?: { message_id?: string } };
          if (data.type === "new_invoice") {
            fire(data.data?.message_id);
          }
        }
      } catch {
        // network error — ignore, next poll will retry
      }
    }

    connect();
    // Start polling after a short delay so SSE gets first shot.
    const pollStart = setTimeout(() => {
      poll(); // immediate first poll
      pollTimer = setInterval(poll, 5000);
    }, 2000);

    return () => {
      destroyed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      if (pollTimer !== null) clearInterval(pollTimer);
      clearTimeout(pollStart);
      es?.close();
    };
  }, []);
}
