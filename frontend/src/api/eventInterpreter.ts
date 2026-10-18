import type { AdkEvent, InterruptInfo, PipelineState } from "./types";
import { stageForAuthor, type StageId } from "../store/stageMap";

export const A2UI_MIME = "application/a2ui+json";
const REQUEST_INPUT = "adk_request_input";

/** Join all plain-text parts of an event (skips function/inline-data parts). */
export function getText(ev: AdkEvent): string {
  const parts = ev.content?.parts ?? [];
  return parts
    .map((p) => p.text)
    .filter((t): t is string => typeof t === "string" && t.length > 0)
    .join("");
}

/** The state delta carried by this event, if any. */
export function getStateDelta(ev: AdkEvent): Partial<PipelineState> | null {
  const delta = ev.actions?.stateDelta;
  return delta && Object.keys(delta).length > 0 ? (delta as Partial<PipelineState>) : null;
}

/** Decode and return all A2UI message objects embedded in this event. */
export function getA2uiMessages(ev: AdkEvent): Record<string, unknown>[] {
  const parts = ev.content?.parts ?? [];
  const out: Record<string, unknown>[] = [];
  for (const part of parts) {
    if (part.inlineData?.mimeType === A2UI_MIME && part.inlineData.data) {
      try {
        const json = decodeBase64Utf8(part.inlineData.data);
        out.push(JSON.parse(json) as Record<string, unknown>);
      } catch {
        /* skip malformed A2UI part */
      }
    }
  }
  return out;
}

/**
 * The node that produced this event. ADK sets `author` to the workflow name;
 * the actual node is the leaf of `nodeInfo.path`
 * (e.g. "...workflow@1/fetch_invoice_email@1" -> "fetch_invoice_email").
 */
export function nodeName(ev: AdkEvent): string | undefined {
  const path = ev.nodeInfo?.path;
  if (path) {
    const leaf = path.split("/").pop() ?? "";
    const name = leaf.replace(/@\d+$/, "");
    if (name) return name;
  }
  return ev.author;
}

/** Detect a HITL interrupt (RequestInput) in this event. */
export function getInterrupt(ev: AdkEvent): InterruptInfo | null {
  const parts = ev.content?.parts ?? [];
  for (const part of parts) {
    const fc = part.functionCall;
    if (fc && fc.name === REQUEST_INPUT && fc.id) {
      return {
        interruptId: fc.id,
        invocationId: ev.invocationId,
        message: (fc.args?.message as string | undefined) ?? undefined,
        kind: kindForNode(nodeName(ev)),
      };
    }
  }
  // Fallback: long_running_tool_ids present without an explicit functionCall.
  if (ev.longRunningToolIds && ev.longRunningToolIds.length > 0) {
    return {
      interruptId: ev.longRunningToolIds[0],
      invocationId: ev.invocationId,
      kind: kindForNode(nodeName(ev)),
    };
  }
  return null;
}

export function getStage(ev: AdkEvent): StageId | null {
  return stageForAuthor(nodeName(ev));
}

export function getError(ev: AdkEvent): string | null {
  return ev.error ?? ev.errorMessage ?? null;
}

export interface FriendlyError {
  /** Short, user-facing headline. */
  message: string;
  /** Optional secondary line with guidance. */
  hint?: string;
  /** Whether re-running the pipeline is likely to succeed. */
  retryable: boolean;
}

/**
 * Map a raw backend error string into a friendly, demo-safe message. The most
 * common failure when the extraction agent calls Gemini is a Vertex AI quota
 * block (`429 RESOURCE_EXHAUSTED`), which otherwise surfaces as a wall of raw
 * provider text. Keep this purely presentational — the raw error is still
 * logged to the console by the store.
 */
export function classifyError(raw: string | null | undefined): FriendlyError {
  const t = (raw ?? "").toLowerCase();
  if (
    t.includes("resource_exhausted") ||
    t.includes("429") ||
    t.includes("quota") ||
    t.includes("rate limit")
  ) {
    return {
      message: "The extraction model is temporarily rate-limited.",
      hint: "Vertex AI quota was hit — wait a few seconds, then try again.",
      retryable: true,
    };
  }
  if (
    t.includes("permission") ||
    t.includes("unauthorized") ||
    t.includes("unauthenticated") ||
    t.includes("403")
  ) {
    return {
      message: "The agent couldn't authenticate to Google Cloud.",
      hint: "Check application-default credentials (gcloud auth application-default login).",
      retryable: false,
    };
  }
  if (
    t.includes("deadline") ||
    t.includes("timeout") ||
    t.includes("unavailable") ||
    t.includes("503") ||
    t.includes("500")
  ) {
    return {
      message: "The model timed out or was briefly unavailable.",
      hint: "This is usually transient — try again.",
      retryable: true,
    };
  }
  const trimmed = (raw ?? "Something went wrong while processing the invoice.").trim();
  return {
    message: trimmed.length > 240 ? `${trimmed.slice(0, 240)}…` : trimmed,
    retryable: true,
  };
}

function kindForNode(name?: string): InterruptInfo["kind"] {
  if (name === "human_approval_gate") return "approval";
  if (name === "review_email_draft") return "email_review";
  return "unknown";
}

/** Decode a base64 string (ADK Blob.data) into a UTF-8 string. */
function decodeBase64Utf8(b64: string): string {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}
