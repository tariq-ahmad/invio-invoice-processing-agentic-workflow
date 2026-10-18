import { useEffect, useRef } from "react";

/** Fire a celebratory confetti burst once when `trigger` becomes true.
 * canvas-confetti is loaded on demand — it's only needed on a successful post,
 * so it stays out of the initial bundle. */
export function useConfetti(trigger: boolean) {
  const fired = useRef(false);
  useEffect(() => {
    if (!trigger || fired.current) return;
    fired.current = true;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    void import("canvas-confetti").then(({ default: confetti }) => {
      const end = Date.now() + 700;
      const colors = ["#a78bfa", "#7c5cf5", "#ddd6fe", "#34d399"];
      (function frame() {
        confetti({ particleCount: 3, angle: 60, spread: 55, origin: { x: 0 }, colors });
        confetti({ particleCount: 3, angle: 120, spread: 55, origin: { x: 1 }, colors });
        if (Date.now() < end) requestAnimationFrame(frame);
      })();
    });
  }, [trigger]);
}
