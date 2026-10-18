import { AnimatePresence, motion } from "framer-motion";
import { useToasts, type ToastTone } from "../../store/useToasts";
import { cn } from "../../lib/utils";

const TONE: Record<ToastTone, string> = {
  info: "border-line-strong",
  success: "border-done-500/40",
  error: "border-rose-400/45",
};
const DOT: Record<ToastTone, string> = { info: "bg-brand-400", success: "bg-done-400", error: "bg-rose-400" };

/** Small, non-blocking confirmations ("Downloaded", "Couldn't read that file"). */
export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div className="no-print pointer-events-none fixed bottom-5 right-5 z-[60] flex w-80 flex-col gap-2" aria-live="polite">
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <motion.div
            key={t.id}
            layout="position"
            initial={{ opacity: 0, y: 10, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 6 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className={cn("card pointer-events-auto flex items-start gap-2.5 rounded-xl px-3.5 py-2.5", TONE[t.tone])}
          >
            <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", DOT[t.tone])} />
            <p className="min-w-0 flex-1 text-sm leading-snug text-slate-100">{t.text}</p>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss"
              className="shrink-0 text-slate-500 transition hover:text-slate-200"
            >
              ×
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
