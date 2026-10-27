/** Remark categories used for panel + app ordering. */

export type ConfigCategory = 'direct' | 'tunnel' | 'gaming' | 'other';

const SLOT = 5;

export function detectConfigCategory(remark: string | null | undefined): ConfigCategory {
  const t = String(remark || '').toLowerCase();
  // Prefer specific tags first
  if (/گیمینگ|gaming|\bgame\b|گیم\b/.test(t)) return 'gaming';
  if (/تانل|تونل|tunnel/.test(t)) return 'tunnel';
  if (/مستقیم|mostaghim|direct/.test(t)) return 'direct';
  return 'other';
}

/**
 * Arrange successful rows: up to 5 best direct → 5 tunnel → 5 gaming,
 * then remaining by quality. Failures/untested stay at the end.
 *
 * Quality: more successful devices first, then lower avg ping.
 */
export function arrangeByCategorySlots<
  T extends {
    avg_ping: number;
    device_count?: number;
    label?: string;
    remark?: string;
  },
>(items: T[]): T[] {
  const ok: T[] = [];
  const rest: T[] = [];
  for (const it of items) {
    if (it.avg_ping > 0) ok.push(it);
    else rest.push(it);
  }

  const quality = (a: T, b: T) => {
    const da = a.device_count ?? 1;
    const db = b.device_count ?? 1;
    if (da !== db) return db - da;
    if (a.avg_ping !== b.avg_ping) return a.avg_ping - b.avg_ping;
    return 0;
  };

  const buckets: Record<ConfigCategory, T[]> = {
    direct: [],
    tunnel: [],
    gaming: [],
    other: [],
  };
  for (const it of ok) {
    const cat = detectConfigCategory(it.label || it.remark || '');
    buckets[cat].push(it);
  }
  for (const cat of Object.keys(buckets) as ConfigCategory[]) {
    buckets[cat].sort(quality);
  }

  const picked = new Set<T>();
  const head: T[] = [];
  for (const cat of ['direct', 'tunnel', 'gaming'] as const) {
    for (const it of buckets[cat].slice(0, SLOT)) {
      head.push(it);
      picked.add(it);
    }
  }

  const leftover = ok.filter((it) => !picked.has(it)).sort(quality);
  return [...head, ...leftover, ...rest];
}
