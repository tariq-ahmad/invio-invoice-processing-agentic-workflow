import { useEffect } from "react";
import { useThemeStore } from "../store/useThemeStore";

/** Paper is always light: switch the theme to light for the duration of a print
 * (the Export menu or Ctrl+P) and restore the user's choice afterwards. */
export function usePrintTheme(): void {
  useEffect(() => {
    const root = document.documentElement;
    const before = () => {
      root.dataset.theme = "light";
      root.style.colorScheme = "light";
    };
    const after = () => {
      const t = useThemeStore.getState().theme;
      root.dataset.theme = t;
      root.style.colorScheme = t;
    };
    window.addEventListener("beforeprint", before);
    window.addEventListener("afterprint", after);
    return () => {
      window.removeEventListener("beforeprint", before);
      window.removeEventListener("afterprint", after);
    };
  }, []);
}
