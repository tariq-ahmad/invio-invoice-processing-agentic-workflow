/**
 * Per-agent model selection (Pro / Flash / Flash-Lite). The UI picks a model per
 * agent and confirms; confirm() POSTs to the backend which re-points the live
 * ADK agents. Confirmed selection is persisted to localStorage and synced from
 * the server on load (the backend resets to flash-lite on restart).
 */
import { create } from "zustand";

export type AgentKey = "extraction" | "draft_email";
export type Models = Record<AgentKey, string>;

export const MODEL_OPTIONS: { id: string; label: string }[] = [
  { id: "gemini-3.8-flash",      label: "3.8 Flash" },
  { id: "gemini-3.5-flash-lite", label: "3.5 Flash-Lite" },
];

export function modelLabel(id?: string): string {
  return MODEL_OPTIONS.find((m) => m.id === id)?.label ?? id ?? "—";
}

const STORAGE_KEY = "apco_agent_models_v1";
const DEFAULT: Models = {
  extraction: "gemini-3.5-flash-lite",
  draft_email: "gemini-3.5-flash-lite",
};

function loadConfirmed(): Models {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT };
    return { ...DEFAULT, ...(JSON.parse(raw) as Partial<Models>) };
  } catch {
    return { ...DEFAULT };
  }
}

interface ModelConfig {
  selected: Models;
  confirmed: Models;
  saving: boolean;
  justSaved: boolean;
  dirty: () => boolean;
  setSelected: (key: AgentKey, model: string) => void;
  confirm: () => Promise<void>;
  syncFromServer: () => Promise<void>;
  resetToDefault: () => Promise<void>;
}

export const useModelConfig = create<ModelConfig>((set, get) => {
  const confirmed = loadConfirmed();
  return {
    selected: { ...confirmed },
    confirmed,
    saving: false,
    justSaved: false,

    dirty() {
      const { selected, confirmed } = get();
      return (Object.keys(selected) as AgentKey[]).some((k) => selected[k] !== confirmed[k]);
    },

    setSelected(key, model) {
      set((s) => ({ selected: { ...s.selected, [key]: model }, justSaved: false }));
    },

    async confirm() {
      const sel = get().selected;
      set({ saving: true, justSaved: false });
      try {
        const res = await fetch("/config/model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(sel),
        });
        const data = (await res.json()) as { status?: string; models?: Models };
        if (res.ok && data.status === "ok" && data.models) {
          try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(data.models));
          } catch {
            /* ignore */
          }
          set({ confirmed: { ...data.models }, selected: { ...data.models }, justSaved: true });
        }
      } catch {
        /* leave selection as-is; user can retry */
      } finally {
        set({ saving: false });
      }
    },

    async syncFromServer() {
      try {
        const res = await fetch("/config/model", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as { models?: Models };
        if (data.models) {
          try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(data.models));
          } catch {
            /* ignore */
          }
          set({ confirmed: { ...data.models }, selected: { ...data.models } });
        }
      } catch {
        /* offline / backend down — keep persisted values */
      }
    },

    // Force both agents back to the ambient default (Flash-Lite). Used when the
    // user leaves manual mode so autonomous/event-driven runs are always cheap
    // and consistent regardless of what was last selected in a demo.
    async resetToDefault() {
      const alreadyDefault =
        get().confirmed.extraction === DEFAULT.extraction &&
        get().confirmed.draft_email === DEFAULT.draft_email;
      // Optimistically reflect the default in the UI immediately.
      set({ confirmed: { ...DEFAULT }, selected: { ...DEFAULT } });
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(DEFAULT));
      } catch {
        /* ignore */
      }
      if (alreadyDefault) return; // backend already on default; skip the POST
      try {
        await fetch("/config/model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(DEFAULT),
        });
      } catch {
        /* backend down — UI still shows default; next sync reconciles */
      }
    },
  };
});
