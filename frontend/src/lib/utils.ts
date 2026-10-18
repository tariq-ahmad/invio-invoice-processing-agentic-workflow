let _counter = 0;

/** Concatenate truthy class strings. Simpler alternative to clsx/twMerge. */
export function cn(...args: (string | boolean | null | undefined)[]): string {
  return args.filter(Boolean).join(" ") as string;
}

/** Generate a collision-resistant incremental id with a readable prefix. */
export function nextId(prefix: string): string {
  return `${prefix}-${++_counter}`;
}

/** Return a stable ID stored in localStorage under `key`, creating it on first call. */
export function persistedId(key: string): string {
  try {
    const stored = localStorage.getItem(key);
    if (stored) return stored;
    const id = `${key}-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(key, id);
    return id;
  } catch {
    return `${key}-fallback`;
  }
}
