/** Tiny shared flags so the run store can behave differently during a replay
 * without importing the replay controller (which imports the store). */
export const replayHooks: {
  active: boolean;
  /** Set by the controller while it waits at a human-decision boundary; the
   * store calls it when the user continues. */
  resume: (() => void) | null;
} = { active: false, resume: null };
