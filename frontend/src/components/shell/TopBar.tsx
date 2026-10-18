import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore } from "../../store/usePipelineStore";
import { useThemeStore } from "../../store/useThemeStore";
import { SessionCostMeter } from "./SessionCostMeter";
import { ElapsedTime } from "./ElapsedTime";
import { StatsDrawer } from "./StatsDrawer";
import { InvioMark } from "../common/InvioMark";
import { useAlertPrefs } from "../../store/useAlertPrefs";
import { exitReplay, useReplay } from "../../replay/controller";

function MoonIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="white">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="white">
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="4" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="12" y1="20" x2="12" y2="22" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="2" y1="12" x2="4" y2="12" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="20" y1="12" x2="22" y2="12" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" stroke="white" strokeWidth="2" strokeLinecap="round" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" stroke="white" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function ThemeToggle() {
  const { theme, toggleTheme } = useThemeStore();
  const isDark = theme === "dark";
  return (
    <button
      onClick={toggleTheme}
      role="switch"
      aria-checked={!isDark}
      aria-label="Toggle light/dark mode"
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className="relative flex h-6 w-11 cursor-pointer items-center rounded-full border border-line-strong bg-wash px-0.5 transition hover:border-brand-400/40"
    >
      <motion.span
        layout
        transition={{ type: "spring", stiffness: 500, damping: 30 }}
        className="flex h-5 w-5 items-center justify-center rounded-full bg-gradient-to-br from-brand-400 to-brand-600 shadow-sm"
        style={{ marginLeft: isDark ? 0 : "calc(100% - 1.25rem)" }}
      >
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={theme}
            initial={{ opacity: 0, rotate: -30, scale: 0.7 }}
            animate={{ opacity: 1, rotate: 0, scale: 1 }}
            exit={{ opacity: 0, rotate: 30, scale: 0.7 }}
            transition={{ duration: 0.18 }}
            className="block"
          >
            {isDark ? <MoonIcon /> : <SunIcon />}
          </motion.span>
        </AnimatePresence>
      </motion.span>
    </button>
  );
}

function IconButton({
  label,
  onClick,
  pressed,
  children,
}: {
  label: string;
  onClick: () => void;
  pressed?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      aria-pressed={pressed}
      className="grid h-8 w-8 place-items-center rounded-lg border border-line-strong bg-wash text-slate-300 transition hover:border-line-bold hover:bg-wash-strong hover:text-slate-50 aria-pressed:border-brand-400/40 aria-pressed:bg-brand-500/15 aria-pressed:text-brand-200"
    >
      {children}
    </button>
  );
}

const svgProps = {
  width: 15,
  height: 15,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

function NotifyToggle() {
  const on = useAlertPrefs((s) => s.notify);
  const toggle = useAlertPrefs((s) => s.toggleNotify);
  return (
    <IconButton
      label={on ? "Desktop notifications on (click to turn off)" : "Notify me when an approval needs me"}
      pressed={on}
      onClick={() => void toggle()}
    >
      <svg {...svgProps}>
        <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
        <path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" />
        {!on && <path d="M3 3l18 18" />}
      </svg>
    </IconButton>
  );
}

export function TopBar() {
  const reset = usePipelineStore((s) => s.reset);
  const status = usePipelineStore((s) => s.status);
  const replaying = useReplay((s) => s.active);
  const [statsOpen, setStatsOpen] = useState(false);

  return (
    <header className="no-print glass-blur sticky top-0 z-40 flex items-center justify-between gap-4 border-b border-line px-6 py-3">
      <div className="flex min-w-0 items-center gap-4">
        <div className="flex items-center gap-3">
          <motion.div
            initial={{ scale: 0.9, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
            className="relative h-9 w-9 shrink-0 rounded-xl shadow-lg shadow-brand-500/25 ring-1 ring-white/20"
          >
            <InvioMark size={36} className="block" />
          </motion.div>
          <h1 className="flex min-w-0 items-baseline gap-2.5">
            <span className="text-gradient text-[17px] font-semibold tracking-tight">Invio</span>
            <span aria-hidden="true" className="hidden h-4 w-px self-center bg-line-strong sm:block" />
            <span className="hidden truncate text-[13px] font-medium text-slate-400 sm:block">
              Graph Based Agentic Workflow
            </span>
          </h1>
        </div>
      </div>

      <div className="flex items-center gap-2.5">
        <SessionCostMeter />
        <ElapsedTime />
        <span aria-hidden="true" className="mx-0.5 hidden h-5 w-px bg-line-strong lg:block" />
        <IconButton label="Insights" onClick={() => setStatsOpen(true)}>
          <svg {...svgProps}>
            <path d="M4 20V10m6 10V4m6 16v-7m4 7H2" />
          </svg>
        </IconButton>
        <NotifyToggle />
        <ThemeToggle />
        <button
          onClick={replaying ? exitReplay : reset}
          disabled={status === "streaming" && !replaying}
          className="rounded-lg border border-line-strong bg-wash px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-line-bold hover:bg-wash-strong hover:text-slate-100 active:scale-[0.97] disabled:opacity-40 disabled:active:scale-100"
        >
          {replaying ? "Exit replay" : "New session"}
        </button>
      </div>
      <StatsDrawer open={statsOpen} onClose={() => setStatsOpen(false)} />
    </header>
  );
}
