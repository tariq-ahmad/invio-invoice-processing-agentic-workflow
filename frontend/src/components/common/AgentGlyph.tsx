/**
 * Official ADK agent logo — bicolor oval head (blue left / green right), blue
 * eyes, yellow [ bracket, red <> chevrons. Used wherever an LLM agent is
 * involved: pipeline AGENT badges, ModelTag, and A2UI card headers.
 *
 * Honors prefers-reduced-motion (renders static).
 */
import { useId } from "react";
import { motion } from "framer-motion";
import { prefersReducedMotion } from "../../lib/motion";

interface AgentGlyphProps {
  size?: number;
  /** Set false to render static (e.g. for a dimmed/pending badge). */
  animate?: boolean;
  className?: string;
}

export function AgentGlyph({ size = 14, animate = true, className }: AgentGlyphProps) {
  const gid = useId();
  const pulse = animate && !prefersReducedMotion();

  return (
    <motion.svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      aria-hidden="true"
      style={{
        filter: pulse
          ? "drop-shadow(0 0 5px rgba(66,133,244,0.75))"
          : "drop-shadow(0 0 2px rgba(66,133,244,0.45))",
      }}
      animate={pulse ? { scale: [1, 1.12, 1] } : { scale: 1 }}
      transition={
        pulse
          ? { duration: 2.4, repeat: Infinity, repeatDelay: 1.2, ease: "easeInOut" }
          : { duration: 0 }
      }
    >
      <defs>
        <clipPath id={`${gid}l`}>
          <rect x="0" y="0" width="12" height="24" />
        </clipPath>
        <clipPath id={`${gid}r`}>
          <rect x="12" y="0" width="12" height="24" />
        </clipPath>
      </defs>

      {/* Head — left half blue arc */}
      <ellipse
        cx="12" cy="8.5" rx="8.75" ry="5"
        stroke="#4285F4" strokeWidth="3" fill="none"
        clipPath={`url(#${gid}l)`}
      />
      {/* Head — right half green arc */}
      <ellipse
        cx="12" cy="8.5" rx="8.75" ry="5"
        stroke="#34A853" strokeWidth="3" fill="none"
        clipPath={`url(#${gid}r)`}
      />

      {/* Eyes (blue) */}
      <circle cx="9"  cy="8.5" r="1.9" fill="#4285F4" />
      <circle cx="15" cy="8.5" r="1.9" fill="#4285F4" />

      {/* Yellow [ bracket */}
      <path
        d="M6 14.5 L2.8 14.5 L2.8 20 L6 20"
        stroke="#FBBC04" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none"
      />

      {/* Red < chevron */}
      <path
        d="M10.5 14.2 L7.5 17.1 L10.5 20"
        stroke="#EA4335" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none"
      />
      {/* Red > chevron */}
      <path
        d="M14.5 14.2 L17.5 17.1 L14.5 20"
        stroke="#EA4335" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" fill="none"
      />
    </motion.svg>
  );
}
