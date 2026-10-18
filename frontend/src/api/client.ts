import { persistedId } from "../lib/utils";
import type { AdkContent, AdkSession, RunAgentRequest } from "./types";

const APP_NAME = "app"; // must match App(name="app") in app/agent.py

// Same-origin in dev (Vite proxies /apps, /run_sse, ... to :8000) and in prod
// (SPA served by the ADK server). Override with VITE_ADK_BASE if needed.
const BASE = (import.meta.env.VITE_ADK_BASE as string | undefined) ?? "";

/** URL for the original invoice attachment, re-fetched fresh from AgentMail by
 * message id (see app/fast_api_app.py's /invoices/{message_id}/pdf). */
export function invoicePdfUrl(messageId: string): string {
  return `${BASE}/invoices/${encodeURIComponent(messageId)}/pdf`;
}

/** Thin client over the ADK FastAPI surface. */
export class AdkClient {
  readonly appName = APP_NAME;
  readonly userId: string;
  sessionId: string | null = null;

  constructor() {
    this.userId = persistedId("ap-copilot-user");
  }

  private url(path: string): string {
    return `${BASE}${path}`;
  }

  /** Create a fresh session (idempotent per client instance). */
  async ensureSession(force = false): Promise<string> {
    if (this.sessionId && !force) return this.sessionId;
    const res = await fetch(
      this.url(`/apps/${this.appName}/users/${this.userId}/sessions`),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    );
    if (!res.ok) {
      throw new Error(`Failed to create session: ${res.status} ${await res.text()}`);
    }
    const session = (await res.json()) as AdkSession;
    this.sessionId = session.id;
    return session.id;
  }

  /** Start (or continue) the pipeline with a user text message. Returns the
   * poll token driving pipeline-progress polling (see pollParser.ts) — the
   * direct /run_sse response is still sent (ADK requires it to invoke the
   * agent) but is drained in the background, unread, since Cloud Run fully
   * buffers it over HTTP/2 until the whole run completes. */
  async run(text: string): Promise<string> {
    const sessionId = await this.ensureSession();
    const body: RunAgentRequest = {
      app_name: this.appName,
      user_id: this.userId,
      session_id: sessionId,
      new_message: { role: "user", parts: [{ text }] },
      streaming: false,
    };
    return this.postSse(body);
  }

  /** Resume an interrupt with a value (string or structured object). Returns
   * the poll token (see run() above). */
  async resume(
    interruptId: string,
    value: unknown,
    invocationId?: string,
  ): Promise<string> {
    const sessionId = await this.ensureSession();
    // For structured payloads we send the object directly as the response
    // (ADK passes a multi-key dict through unchanged); for scalars we wrap as
    // { result: value } which ADK unwraps to the bare value.
    const response =
      value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : { result: value };
    const newMessage: AdkContent = {
      role: "user",
      parts: [
        {
          functionResponse: {
            id: interruptId,
            name: "adk_request_input",
            response,
          },
        },
      ],
    };
    const body: RunAgentRequest = {
      app_name: this.appName,
      user_id: this.userId,
      session_id: sessionId,
      new_message: newMessage,
      streaming: false,
      ...(invocationId ? { invocation_id: invocationId } : {}),
    };
    return this.postSse(body);
  }

  /** Reset the conversation by creating a new session. */
  async reset(): Promise<void> {
    await this.ensureSession(true);
  }

  /** Fires the /run_sse POST (required to actually invoke the agent) and
   * returns a poll token for pipeline-progress polling — WITHOUT awaiting the
   * fetch's own promise. Confirmed empirically: in the browser, that promise
   * doesn't resolve until Cloud Run has buffered a large fraction of the
   * whole response (tens of seconds in), since it fully buffers /run_sse over
   * HTTP/2 until the stream closes. Awaiting it here would delay polling by
   * the same amount, defeating the point. The request is still sent
   * immediately (fetch() starts the network request synchronously); we just
   * don't block on its response. Never cancelled once sent — aborting it
   * could be read by the backend as a client disconnect and cancel the run. */
  private postSse(body: RunAgentRequest): string {
    const pollToken =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `poll-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    fetch(this.url("/run_sse"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-Poll-Token": pollToken,
      },
      body: JSON.stringify(body),
    })
      .then(async (res) => {
        if (!res.ok || !res.body) {
          console.error(`run_sse failed: ${res.status}`);
          return;
        }
        const reader = res.body.getReader();
        try {
          while (true) {
            const { done } = await reader.read();
            if (done) break;
          }
        } catch {
          // Connection ended — nothing to do, polling is the source of truth.
        }
      })
      .catch((e) => console.error("run_sse request failed:", e));
    return pollToken;
  }
}
