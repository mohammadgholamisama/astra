/** Preset cycle lengths — minutes + hours. Default = 5 minutes. */
export const CYCLE_OPTIONS = [
  { ms: 2 * 60 * 1000, short: '۲د', amount: 2, unit: 'min' as const, title: '۲ دقیقه' },
  { ms: 5 * 60 * 1000, short: '۵د', amount: 5, unit: 'min' as const, title: '۵ دقیقه' },
  { ms: 10 * 60 * 1000, short: '۱۰د', amount: 10, unit: 'min' as const, title: '۱۰ دقیقه' },
  { ms: 15 * 60 * 1000, short: '۱۵د', amount: 15, unit: 'min' as const, title: '۱۵ دقیقه' },
  { ms: 30 * 60 * 1000, short: '۳۰د', amount: 30, unit: 'min' as const, title: '۳۰ دقیقه' },
  { ms: 60 * 60 * 1000, short: '۱س', amount: 1, unit: 'hour' as const, title: '۱ ساعت' },
  { ms: 2 * 60 * 60 * 1000, short: '۲س', amount: 2, unit: 'hour' as const, title: '۲ ساعت' },
  { ms: 3 * 60 * 60 * 1000, short: '۳س', amount: 3, unit: 'hour' as const, title: '۳ ساعت' },
  { ms: 6 * 60 * 60 * 1000, short: '۶س', amount: 6, unit: 'hour' as const, title: '۶ ساعت' },
] as const;

export type CycleOption = (typeof CYCLE_OPTIONS)[number];

export const CYCLE_MINUTE_OPTIONS = CYCLE_OPTIONS.filter((o) => o.unit === 'min');
export const CYCLE_HOUR_OPTIONS = CYCLE_OPTIONS.filter((o) => o.unit === 'hour');

export const DEFAULT_CYCLE_MS = 5 * 60 * 1000;

const ALLOWED = new Set<number>(CYCLE_OPTIONS.map((o) => o.ms));

export function normalizeCycleMs(ms: unknown): number {
  const n = typeof ms === 'number' ? ms : Number(ms);
  if (!Number.isFinite(n) || n < 60_000) return DEFAULT_CYCLE_MS;
  if (ALLOWED.has(n)) return n;
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

export function cycleOptionOf(ms: number): CycleOption {
  const n = normalizeCycleMs(ms);
  return (
    CYCLE_OPTIONS.find((o) => o.ms === n) ??
    CYCLE_OPTIONS.find((o) => o.ms === DEFAULT_CYCLE_MS)!
  );
}

/** Full Persian label, e.g. «۵ دقیقه» / «۲ ساعت». */
export function cycleIntervalLabel(ms: number): string {
  return cycleOptionOf(ms).title;
}

/** @deprecated use cycleIntervalLabel — kept for older call sites. */
export function cycleMinutesLabel(ms: number): string {
  const opt = cycleOptionOf(ms);
  if (opt.unit === 'hour') return String(opt.amount * 60);
  return String(opt.amount);
}
