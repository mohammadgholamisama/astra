/** Remark categories used for panel + app ordering. */

export type ConfigCategory = 'direct' | 'tunnel' | 'gaming' | 'other';

const SLOT = 5;

export function detectConfigCategory(remark: string | null | undefined): ConfigCategory {
  const t = String(remark || '').toLowerCase();
  if (/گیمینگ|gaming|\bgame\b|گیم\b/.test(t)) return 'gaming';
  if (/تانل|تونل|tunnel/.test(t)) return 'tunnel';
  if (/مستقیم|mostaghim|direct/.test(t)) return 'direct';
  return 'other';
}

type Rankable = {
  avg_ping: number;
  device_count?: number;
  download_kbps?: number;
  upload_kbps?: number;
  packet_loss_pct?: number;
  label?: string;
  remark?: string;
};

function hasSpeed(it: Rankable): boolean {
  return (it.download_kbps ?? 0) > 0 || (it.upload_kbps ?? 0) > 0;
}

function hasPacket(it: Rankable): boolean {
  return it.packet_loss_pct != null && it.packet_loss_pct >= 0;
}

function lossKey(it: Rankable): number {
  const v = it.packet_loss_pct;
  if (v == null || v < 0) return 101;
  return v;
}

/**
 * Quality: more devices → lower packet loss → higher download → higher upload → lower ping.
 */
function quality(a: Rankable, b: Rankable): number {
  const da = a.device_count ?? 1;
  const db = b.device_count ?? 1;
  if (da !== db) return db - da;

  const packetAware = hasPacket(a) || hasPacket(b);
  if (packetAware) {
    const la = lossKey(a);
    const lb = lossKey(b);
    if (la !== lb) return la - lb;
  }

  const speedAware = hasSpeed(a) || hasSpeed(b) || packetAware;
  if (speedAware) {
    const dlA = a.download_kbps ?? 0;
    const dlB = b.download_kbps ?? 0;
    if (dlA !== dlB) return dlB - dlA;
    const ulA = a.upload_kbps ?? 0;
    const ulB = b.upload_kbps ?? 0;
    if (ulA !== ulB) return ulB - ulA;
  }

  if (a.avg_ping !== b.avg_ping) return a.avg_ping - b.avg_ping;
  return 0;
}

/**
 * Arrange successful rows: up to 5 best direct → 5 tunnel → 5 gaming,
 * then remaining by quality. Failures/untested stay at the end.
 */
export function arrangeByCategorySlots<T extends Rankable>(items: T[]): T[] {
  const ok: T[] = [];
  const rest: T[] = [];
  for (const it of items) {
    if (
      it.avg_ping > 0 ||
      hasSpeed(it) ||
      (hasPacket(it) && (it.packet_loss_pct as number) < 100)
    ) {
      ok.push(it);
    } else {
      rest.push(it);
    }
  }

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
