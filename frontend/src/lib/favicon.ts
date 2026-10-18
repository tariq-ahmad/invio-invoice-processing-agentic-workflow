import { INVIO_MARK_INNER } from "./invioMark";

/** The app icon as an inline SVG, optionally with a status dot in the corner —
 * used to signal "needs you" on a background tab. */
function svg(dot?: string): string {
  const dotEl = dot ? `<circle cx="50" cy="14" r="11" fill="${dot}" stroke="#ffffff" stroke-width="3"/>` : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">${INVIO_MARK_INNER}${dotEl}</svg>`;
}

export const DOT = { attention: "#f59e0b", error: "#f43f5e", done: "#10b981" } as const;

/** Set the tab icon (with an optional corner dot). Safe to call repeatedly. */
export function setFavicon(dot?: string): void {
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/svg+xml";
    document.head.appendChild(link);
  }
  link.href = `data:image/svg+xml,${encodeURIComponent(svg(dot))}`;
}
