/**
 * Translates an A2UI client action (emitted by the @a2ui/react renderer when a
 * Button is clicked) into the resume payload the ADK HITL node expects.
 *
 * Backend action names (see app/a2ui_views.py):
 *   approve_invoice / reject_invoice  -> approval_decision interrupt
 *   send_email / revise_email         -> email_review_{n} interrupt
 *
 * The action's `context` carries the interrupt_id plus the data the node needs
 * (decision word, and for the email: the edited subject/body or feedback text,
 * resolved from the surface's data model by the renderer).
 */

export interface A2uiClientAction {
  name: string;
  surfaceId: string;
  sourceComponentId: string;
  timestamp?: string;
  context: Record<string, unknown>;
}

export interface ResumeIntent {
  interruptId: string;
  /** string for simple decisions, object for structured (edited email) payloads */
  value: unknown;
}

export function actionToResume(
  action: A2uiClientAction,
  fallbackInterruptId?: string,
): ResumeIntent | null {
  const ctx = action.context ?? {};
  const interruptId =
    (ctx.interrupt_id as string | undefined) ?? fallbackInterruptId ?? "";
  if (!interruptId) return null;

  switch (action.name) {
    case "approve_invoice":
      return { interruptId, value: "approve" };
    case "reject_invoice":
      return { interruptId, value: "reject" };
    case "send_email":
      return {
        interruptId,
        value: {
          decision: "send",
          subject: String(ctx.subject ?? ""),
          body: String(ctx.body ?? ""),
        },
      };
    case "revise_email":
      return {
        interruptId,
        value: {
          decision: "revise",
          feedback: String(ctx.feedback ?? "").trim() || "Please revise the email.",
          subject: String(ctx.subject ?? ""),
          body: String(ctx.body ?? ""),
        },
      };
    default:
      // Unknown action: pass the decision through if present.
      return { interruptId, value: String(ctx.decision ?? action.name) };
  }
}
