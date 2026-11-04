/** Remark / folder categories used for panel + app ordering. */

export type ConfigCategory = 'direct' | 'tunnel' | 'gaming' | 'other';

/** Layout quotas — keep in sync with backend ping_category_order.py */
export const DIRECT_SLOT = 20;
export const TUNNEL_PER_COST = 35;
export const GAMING_SLOT = 9;
export const SUB_MAX_CONFIGS = 99;
/** Tunnel window before gaming so 20 + 70 + 9 = 99 */
export const TUNNEL_BUDGET = SUB_MAX_CONFIGS - DIRECT_SLOT - GAMING_SLOT; // 70

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
  category_name?: string | null;
  usage_cost?: number | null;
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

function costKey(it: Rankable): number {
  const c = it.usage_cost;
  if (c == null || !Number.isFinite(c)) return 1e9;
  return Number(c);
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

function segmentCmp(a: Rankable, b: Rankable): number {
  const ca = costKey(a);
  const cb = costKey(b);
  if (ca !== cb) return ca - cb;
  return quality(a, b);
}

function resolveCat(it: Rankable): ConfigCategory {
  const fromFolder = detectConfigCategory(it.category_name || '');
  if (fromFolder !== 'other') return fromFolder;
  return detectConfigCategory(it.label || it.remark || '');
}

/**
 * Arrange successful rows:
 *   20 مستقیم (cost↑ then quality) →
 *   تانل per usage_cost↑ up to 35 each, total ≤ TUNNEL_BUDGET (70) →
 *   9 گیمینگ → leftover by cost/quality.
 * Failures/untested stay at the end.
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
    buckets[resolveCat(it)].push(it);
  }
  for (const cat of Object.keys(buckets) as ConfigCategory[]) {
    buckets[cat].sort(segmentCmp);
  }

  const picked = new Set<T>();
  const head: T[] = [];

  for (const it of buckets.direct.slice(0, DIRECT_SLOT)) {
    head.push(it);
    picked.add(it);
  }

  // Tunnel tiers by ascending usage_cost — stop at TUNNEL_BUDGET so gaming fits
  let i = 0;
  let tunnelTaken = 0;
  const tunnel = buckets.tunnel;
  while (i < tunnel.length && tunnelTaken < TUNNEL_BUDGET) {
    const cost = costKey(tunnel[i]!);
    let j = i;
    while (j < tunnel.length && costKey(tunnel[j]!) === cost) j += 1;
    const tier = tunnel.slice(i, j);
    let taken = 0;
    for (const it of tier) {
      if (tunnelTaken >= TUNNEL_BUDGET) break;
      if (taken >= TUNNEL_PER_COST) break;
      if (picked.has(it)) continue;
      head.push(it);
      picked.add(it);
      taken += 1;
      tunnelTaken += 1;
    }
    i = j;
  }

  for (const it of buckets.gaming.slice(0, GAMING_SLOT)) {
    if (picked.has(it)) continue;
    head.push(it);
    picked.add(it);
  }

  const leftover = ok.filter((it) => !picked.has(it)).sort(segmentCmp);
  return [...head, ...leftover, ...rest];
}
