import { motion } from "framer-motion";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { fadeUp } from "../../lib/motion";
import { StepHeader } from "../common/StepHeader";
import { STAGES } from "../../store/stageMap";
import type { StageDef, StageId } from "../../store/stageMap";
import { usePipelineStore } from "../../store/usePipelineStore";

function extractText(node: React.ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node && typeof node === "object" && "props" in (node as object)) {
    return extractText((node as { props: { children?: React.ReactNode } }).props.children);
  }
  return "";
}

const markdownComponents: Components = {
  td({ children, ...props }) {
    const text = extractText(children).trim();
    const isFlag = text === "Flag";
    const isPass = text === "Pass";
    return (
      <td
        {...props}
        className={isFlag ? "policy-flag-cell" : isPass ? "policy-pass-cell" : undefined}
      >
        {children}
      </td>
    );
  },
  // The step header (number + badge + narrative) is rendered once, in the
  // card's own header row — any leading "##" heading in the backend markdown
  // would just duplicate the title, so it's suppressed here.
  h2: () => null,
};

// Same accent-bar treatment as the A2UI cards, keyed off the same STAGES
// accent so every step — table-backed or card-backed — uses one shared
// palette instead of separately-chosen colors.
const ACCENT_GRADIENT: Record<StageDef["accent"], string> = {
  brand: "from-brand-400 to-brand-600",
  sky: "from-sky-400/80 via-sky-500/50 to-sky-600/30",
  amber: "from-amber-300/80 via-amber-400/50 to-amber-500/30",
  emerald: "from-done-400/80 via-done-500/50 to-done-600/30",
  rose: "from-rose-400/70 via-rose-500/40 to-accent-500/40",
  violet: "from-violet-400/80 via-violet-500/50 to-violet-600/30",
};

export function MessageBubble({ text, stageId }: { text: string; stageId?: StageId }) {
  const step = usePipelineStore((s) => (stageId ? s.stepNumbers[stageId] : undefined));
  const def = stageId ? STAGES[stageId] : undefined;

  const body = (
    <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {text}
    </Markdown>
  );

  // Every table-backed step gets the same card shell (accent bar, bordered
  // header row, padded body) as the bespoke A2UI cards — no more visual gap
  // between "steps that are cards" and "steps that are plain bubbles".
  if (def) {
    return (
      <motion.div
        variants={fadeUp}
        initial="initial"
        animate="animate"
        exit="exit"
        // shrink-0 + overflow-hidden: same flexbox-crush guard as A2uiHost
        // (see the comment there) — required whenever a card clips a rounded
        // top accent bar inside this scrolling column.
        className="card shrink-0 overflow-hidden rounded-2xl"
      >
        <div className={`h-[3px] w-full bg-gradient-to-r ${ACCENT_GRADIENT[def.accent]}`} />
        <div className="border-b border-line px-4 py-2.5">
          <StepHeader step={step} title={def.label} agent={!!def.agent} narrative={def.narrative} />
        </div>
        <div className="prose-chat max-w-none p-4 text-sm text-slate-200">{body}</div>
      </motion.div>
    );
  }

  // Defensive fallback for any message that doesn't resolve to a known
  // stage (shouldn't happen in practice — every node maps to one).
  return (
    <motion.div
      variants={fadeUp}
      initial="initial"
      animate="animate"
      exit="exit"
      className="prose-chat card max-w-none px-4 py-3 text-sm text-slate-200"
    >
      {body}
    </motion.div>
  );
}
