/**
 * Animated number count-up. Eases from 0 to `value` on mount. Renders the final
 * value instantly when the user prefers reduced motion.
 */
import { useEffect, useRef, useState } from "react";
import { animate } from "framer-motion";
import { prefersReducedMotion } from "../../lib/motion";

interface CountUpProps {
  value: number;
  /** Decimal places to render. */
  decimals?: number;
  /** Prefix (e.g. "USD ") and suffix (e.g. "%"). */
  prefix?: string;
  suffix?: string;
  /** Thousands separators for large currency values. */
  group?: boolean;
  duration?: number;
  className?: string;
}

function format(n: number, decimals: number, group: boolean): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    useGrouping: group,
  });
}

export function CountUp({
  value,
  decimals = 0,
  prefix = "",
  suffix = "",
  group = false,
  duration = 1.1,
  className,
}: CountUpProps) {
  const [display, setDisplay] = useState(() =>
    prefersReducedMotion() ? value : 0,
  );
  const last = useRef(value);

  useEffect(() => {
    if (prefersReducedMotion()) {
      setDisplay(value);
      return;
    }
    const controls = animate(last.current, value, {
      duration,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (v) => setDisplay(v),
    });
    last.current = value;
    return () => controls.stop();
  }, [value, duration]);

  return (
    <span className={className}>
      {prefix}
      {format(display, decimals, group)}
      {suffix}
    </span>
  );
}
