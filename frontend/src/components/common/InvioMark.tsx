import { INVIO_MARK_INNER } from "../../lib/invioMark";

/** The Invio logo mark (see lib/invioMark.ts). Decorative: it sits beside the
 * "Invio" wordmark. Static markup, no user input. */
export function InvioMark({ size = 36, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      aria-hidden="true"
      className={className}
      dangerouslySetInnerHTML={{ __html: INVIO_MARK_INNER }}
    />
  );
}
