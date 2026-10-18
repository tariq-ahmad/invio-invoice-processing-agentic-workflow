/**
 * Small animated clock icon for the TopBar's Elapsed pill — a distinct glyph
 * from AgentGlyph (agent activity) so "time" reads visually separate from
 * "agent activity", while sharing the same size/animate/className API and
 * glow treatment so both read as one TopBar icon family.
 *
 * Honors prefers-reduced-motion (renders static).
 */
import { motion } from "framer-motion";
import { prefersReducedMotion } from "../../lib/motion";

interface ClockGlyphProps {
  size?: number;
  /** Set false to render static (e.g. before a run has started). */
  animate?: boolean;
  className?: string;
}

export function ClockGlyph({ size = 14, animate = true, className }: ClockGlyphProps) {
  const spin = animate && !prefersReducedMotion();

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      aria-hidden="true"
      style={{
        filter: spin
          ? "drop-shadow(0 0 5px rgba(66,133,244,0.75))"
          : "drop-shadow(0 0 2px rgba(66,133,244,0.45))",
      }}
    >
      {/* Face */}
      <circle cx="12" cy="12" r="9" stroke="#7BA0FF" strokeWidth="2" fill="none" />
      {/* Hour hand — fixed */}
      <path d="M12 12 L12 7.5" stroke="#7BA0FF" strokeWidth="1.6" strokeLinecap="round" />
      {/* Second hand — sweeps continuously while animating */}
      <motion.line
        x1="12" y1="12" x2="12" y2="5.5"
        stroke="#4285F4" strokeWidth="1.6" strokeLinecap="round"
        style={{ transformOrigin: "12px 12px" }}
        animate={spin ? { rotate: 360 } : { rotate: 0 }}
        transition={spin ? { duration: 2, repeat: Infinity, ease: "linear" } : { duration: 0 }}
      />
      <circle cx="12" cy="12" r="1.1" fill="#4285F4" />
    </svg>
  );
}
