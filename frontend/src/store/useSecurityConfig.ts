/**
 * Which security controls the backend has switched on (GET /config/security), so
 * the pipeline rail can show the Model Armor scan steps before a run starts. A run
 * that reports a Model Armor verdict shows them regardless (see PipelineTimeline),
 * so a failed fetch only costs the pre-run preview.
 */
import { create } from "zustand";

interface SecurityConfig {
  modelArmorEnabled: boolean;
  load: () => Promise<void>;
}

export const useSecurityConfig = create<SecurityConfig>((set) => ({
  modelArmorEnabled: false,

  async load() {
    try {
      const res = await fetch("/config/security", { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as { model_armor?: { enabled?: boolean } };
      set({ modelArmorEnabled: !!data.model_armor?.enabled });
    } catch {
      /* leave the default; the rail still reacts to a run's own verdicts */
    }
  },
}));
