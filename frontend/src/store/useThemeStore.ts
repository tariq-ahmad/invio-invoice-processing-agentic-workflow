import { create } from "zustand";

type Theme = "dark" | "light";

const STORAGE_KEY = "theme";

function applyTheme(t: Theme) {
  document.documentElement.dataset.theme = t;
  document.documentElement.style.colorScheme = t;
}

const stored = (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? "dark";
applyTheme(stored);

interface ThemeStore {
  theme: Theme;
  toggleTheme: () => void;
}

export const useThemeStore = create<ThemeStore>((set) => ({
  theme: stored,
  toggleTheme: () =>
    set((s) => {
      const next: Theme = s.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      localStorage.setItem(STORAGE_KEY, next);
      return { theme: next };
    }),
}));
