import { MessageProcessor } from "@a2ui/web_core/v0_9";
import { basicCatalog } from "@a2ui/react/v0_9";
import type { A2uiClientAction } from "./actionBridge";

// The concrete SurfaceModel/MessageProcessor types are generic over the
// catalog's ComponentApi; we treat surfaces opaquely (A2uiSurface consumes them).
export type AnySurface = { id: string };

export interface A2uiEngine {
  processMessages: (messages: Record<string, unknown>[]) => void;
  getSurface: (id: string) => AnySurface | undefined;
}

/**
 * Build a MessageProcessor wired to the bundled basic catalog (React impls).
 * `onAction` receives every Button action from every surface; `onSurface` is
 * called once per surface as it is created so the store can render it.
 */
export function createA2uiEngine(
  onAction: (action: A2uiClientAction) => void,
  onSurface: (surface: AnySurface) => void,
): A2uiEngine {
  // basicCatalog is the React component catalog; its id matches the backend's
  // A2UI_CATALOG_ID, so createSurface binds correctly.
  const processor = new MessageProcessor(
    [basicCatalog as never],
    (action: unknown) => onAction(action as A2uiClientAction),
  );
  const surfaces = new Map<string, AnySurface>();

  processor.onSurfaceCreated((surface: AnySurface) => {
    surfaces.set(surface.id, surface);
    onSurface(surface);
  });

  return {
    processMessages: (messages) => processor.processMessages(messages as never),
    getSurface: (id) => surfaces.get(id),
  };
}
