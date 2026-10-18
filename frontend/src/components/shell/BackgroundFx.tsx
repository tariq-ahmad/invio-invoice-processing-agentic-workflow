/** Ambient page backdrop: two soft accent glows over the base surface.
 * Pure static CSS (no blur filters, no animation) so it costs nothing to paint
 * and never competes with the pipeline graph for attention. Colours come from
 * theme tokens, so light/dark need no JS branching. */
export function BackgroundFx() {
  return (
    <div
      aria-hidden="true"
      className="no-print pointer-events-none fixed inset-0 -z-10 bg-ink-950"
      style={{
        backgroundImage: [
          "radial-gradient(60rem 34rem at 8% -12%, var(--page-glow-1), transparent 62%)",
          "radial-gradient(48rem 30rem at 100% 0%, var(--page-glow-2), transparent 60%)",
        ].join(","),
      }}
    />
  );
}
