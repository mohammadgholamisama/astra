import AsyncStorage from '@react-native-async-storage/async-storage';

const KEY = 'v2ray_ping_uptime_v1';

/** Need this many cycles before uptime ranks (new configs = no penalty). */
export const UPTIME_MIN_SAMPLES = 2;

/** Cap history so old noise fades (rolling window of last N cycles). */
export const UPTIME_WINDOW = 20;

export type UptimeEntry = {
  /** Last N cycle outcomes (true = alive that cycle). */
  window: boolean[];
  updatedAt: number;
};

export type UptimeMap = Record<string, UptimeEntry>;

export function entrySeen(e: UptimeEntry | undefined): number {
  return e?.window?.length ?? 0;
}

export function entryAlive(e: UptimeEntry | undefined): number {
  if (!e?.window?.length) return 0;
  return e.window.reduce((n, ok) => n + (ok ? 1 : 0), 0);
}

/** 0–100; undefined if no samples yet. */
export function uptimePct(e: UptimeEntry | undefined): number | undefined {
  const seen = entrySeen(e);
  if (seen < 1) return undefined;
  return Math.round((entryAlive(e) / seen) * 1000) / 10;
}

export function isUptimeReady(e: UptimeEntry | undefined): boolean {
  return entrySeen(e) >= UPTIME_MIN_SAMPLES;
}

/**
 * Rank key (higher better).
 * New / thin history → 100 (optimistic — don't punish fresh configs).
 */
export function uptimeRankKey(e: UptimeEntry | undefined): number {
  if (!isUptimeReady(e)) return 100;
  return uptimePct(e) ?? 100;
}

export async function loadUptimeMap(): Promise<UptimeMap> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as UptimeMap;
    if (!parsed || typeof parsed !== 'object') return {};
    const out: UptimeMap = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (!v || typeof v !== 'object') continue;
      const w = Array.isArray((v as UptimeEntry).window)
        ? (v as UptimeEntry).window.filter((x) => typeof x === 'boolean')
        : [];
      out[k] = { window: w.slice(-UPTIME_WINDOW), updatedAt: Number((v as UptimeEntry).updatedAt) || 0 };
    }
    return out;
  } catch {
    return {};
  }
}

export async function saveUptimeMap(map: UptimeMap): Promise<void> {
  try {
    await AsyncStorage.setItem(KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('saveUptimeMap failed', e);
  }
}

/**
 * Record one cycle outcome per tested hash. Prune keys not in keepHashes.
 */
export function recordUptimeCycle(
  prev: UptimeMap,
  outcomes: Map<string, boolean>,
  keepHashes?: Set<string>,
): UptimeMap {
  const next: UptimeMap = { ...prev };
  const now = Date.now();
  for (const [hash, alive] of outcomes) {
    const prevE = next[hash];
    const window = [...(prevE?.window ?? []), alive].slice(-UPTIME_WINDOW);
    next[hash] = { window, updatedAt: now };
  }
  if (keepHashes) {
    for (const k of Object.keys(next)) {
      if (!keepHashes.has(k)) delete next[k];
    }
  }
  return next;
}
