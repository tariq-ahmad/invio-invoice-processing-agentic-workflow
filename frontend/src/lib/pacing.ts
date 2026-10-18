/** Global time scale for the store's demo pacing. 1 = real time; 0 = no waiting
 * (used to fast-forward a replay). Everything in the run loop that "sleeps to
 * read like real work" goes through sleep() so replay can speed it up. */
export const pacing = { scale: 1 };

export const sleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms * pacing.scale));

export const SPEEDS = [0.5, 1, 2, 4, 0] as const; // 0 = "max"
export type Speed = (typeof SPEEDS)[number];

export const speedLabel = (s: Speed) => (s === 0 ? "Max" : `${s}×`);
export const speedToScale = (s: Speed) => (s === 0 ? 0 : 1 / s);
