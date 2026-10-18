/**
 * Presentation state for the conversation feed — how finished steps are shown,
 * which card the user pinned, and which one to flash after a jump from the graph.
 * Nothing here affects the run itself.
 */
import { create } from "zustand";

export type FeedMode = "auto" | "all" | "focus";

const STORAGE_KEY = "feed:mode";
const MODES: FeedMode[] = ["auto", "all", "focus"];

export function loadMode(): FeedMode {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) as FeedMode | null;
    if (raw && MODES.includes(raw)) return raw;
  } catch {
    /* storage unavailable */
  }
  return "all"; // show every step in full unless the user chose otherwise
}

interface FeedUi {
  mode: FeedMode;
  setMode: (m: FeedMode) => void;
  /** Explicit user choice per card: true = expanded, false = folded. */
  overrides: Record<string, boolean>;
  setFolded: (id: string, folded: boolean) => void;
  /** In focus mode, the card the user jumped to (otherwise the latest is shown). */
  pin: string | null;
  /** Bumped on every jump so the same card can be flashed twice in a row. */
  flash: { id: string; n: number } | null;
  /** Expand a card, pin it for focus mode, and ask the feed to scroll to it. */
  reveal: (id: string) => void;
  /** Forget per-card choices (a new run starts). */
  clear: () => void;
}

export const useFeedUi = create<FeedUi>((set) => ({
  mode: loadMode(),
  setMode(mode) {
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      /* ignore */
    }
    set({ mode, pin: null });
  },
  overrides: {},
  setFolded(id, folded) {
    set((s) => ({ overrides: { ...s.overrides, [id]: !folded } }));
  },
  pin: null,
  flash: null,
  reveal(id) {
    set((s) => ({
      overrides: { ...s.overrides, [id]: true },
      pin: id,
      flash: { id, n: (s.flash?.n ?? 0) + 1 },
    }));
  },
  clear() {
    set({ overrides: {}, pin: null, flash: null });
  },
}));
