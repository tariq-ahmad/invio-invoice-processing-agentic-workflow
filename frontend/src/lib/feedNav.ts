import { usePipelineStore } from "../store/usePipelineStore";
import { useFeedUi } from "../store/useFeedUi";
import type { StageId } from "../store/stageMap";
import { findItemForStage } from "./stageCards";

const FLASH_CLASS = "feed-flash";

/** Scroll a feed element into view and briefly highlight it. Waits a beat so a
 * card that is unfolding has started to take its height first. */
export function flashElement(domId: string, delayMs = 90): void {
  window.setTimeout(() => {
    const el = document.getElementById(domId);
    if (!el) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    el.classList.remove(FLASH_CLASS);
    void el.offsetWidth; // restart the animation if it is already running
    el.classList.add(FLASH_CLASS);
    window.setTimeout(() => el.classList.remove(FLASH_CLASS), 1500);
  }, delayMs);
}

/** Jump from a graph node to the card showing that stage's output. */
export function jumpToStage(stage: StageId): void {
  if (stage === "complete") {
    flashElement("result-card");
    return;
  }
  const { items, state } = usePipelineStore.getState();
  const item = findItemForStage(items, stage, state);
  if (item) useFeedUi.getState().reveal(item.id);
}
