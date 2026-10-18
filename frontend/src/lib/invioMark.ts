/** The Invio mark (64×64): an invoice with a folded corner and text lines, plus a
 * green "processed" check. One source for the top-bar logo and the tab favicon;
 * keep public/favicon.svg in sync when this changes. */
export const INVIO_MARK_INNER =
  `<defs><linearGradient id="invio-g" x1="0" y1="0" x2="1" y2="1">` +
  `<stop offset="0" stop-color="#a78bfa"/><stop offset="1" stop-color="#6a46e8"/></linearGradient></defs>` +
  `<rect width="64" height="64" rx="16" fill="url(#invio-g)"/>` +
  `<path d="M19 11h20l10 10v29a3 3 0 0 1-3 3H19a3 3 0 0 1-3-3V14a3 3 0 0 1 3-3z" fill="#ffffff"/>` +
  `<path d="M39 11v7a3 3 0 0 0 3 3h7z" fill="#6a46e8" fill-opacity=".35"/>` +
  `<rect x="22" y="26" width="17" height="3.5" rx="1.75" fill="#6a46e8" fill-opacity=".6"/>` +
  `<rect x="22" y="33" width="11" height="3.5" rx="1.75" fill="#6a46e8" fill-opacity=".35"/>` +
  `<circle cx="45" cy="47" r="11" fill="#10b981" stroke="#ffffff" stroke-width="2.5"/>` +
  `<path d="M40 47l3.6 3.6L50.5 43" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>`;
