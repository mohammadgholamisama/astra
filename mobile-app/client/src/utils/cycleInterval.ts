/** Preset cycle lengths (ms). Default = 5 minutes. */
export const CYCLE_OPTIONS = [
  { ms: 2 * 60 * 1000, label: '۲', minutes: 2 },
  { ms: 5 * 60 * 1000, label: '۵', minutes: 5 },
  { ms: 10 * 60 * 1000, label: '۱۰', minutes: 10 },
  { ms: 15 * 60 * 1000, label: '۱۵', minutes: 15 },
  { ms: 30 * 60 * 1000, label: '۳۰', minutes: 30 },
] as const;

export const DEFAULT_CYCLE_MS = 5 * 60 * 1000;

const ALLOWED = new Set(CYCLE_OPTIONS.map((o) => o.ms));

export function normalizeCycleMs(ms: unknown): number {
  const n = typeof ms === 'number' ? ms : Number(ms);
  if (!Number.isFinite(n) || n < 60_000) return DEFAULT_CYCLE_MS;
  if (ALLOWED.has(n as (typeof CYCLE_OPTIONS)[number]['ms'])) return n;
  // Snap to nearest preset
  let best = DEFAULT_CYCLE_MS;
  let bestDiff = Infinity;
  for (const o of CYCLE_OPTIONS) {
    const d = Math.abs(o.ms - n);
    if (d < bestDiff) {
      bestDiff = d;
      best = o.ms;
    }
  }
  return best;
}

export function cycleMinutesLabel(ms: number): string {
  const m = Math.round(normalizeCycleMs(ms) / 60_000);
  return String(m);
}
