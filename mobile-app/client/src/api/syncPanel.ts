import {
  DEFAULT_API_KEY,
  DEFAULT_BASE,
  syncPanelRanks,
  type ApiConfig,
  type FingerprintRankPayload,
} from './client';
import AsyncStorage from '@react-native-async-storage/async-storage';

const SETTINGS_KEY = 'v2ray_ping_settings_v1';
const DEVICE_KEY = 'v2ray_ping_device_id';

let cachedApi: ApiConfig | null = null;
let cacheAt = 0;

async function loadApiConfig(force = false): Promise<ApiConfig> {
  const now = Date.now();
  if (!force && cachedApi && now - cacheAt < 60_000) return cachedApi;

  let deviceId = `dev_${Date.now()}`;
  try {
    const existing = await AsyncStorage.getItem(DEVICE_KEY);
    if (existing) deviceId = existing;
    else await AsyncStorage.setItem(DEVICE_KEY, deviceId);
  } catch {
    /* ignore */
  }

  let cfg: ApiConfig = {
    baseUrl: DEFAULT_BASE,
    apiKey: DEFAULT_API_KEY,
    deviceId,
  };
  try {
    const raw = await AsyncStorage.getItem(SETTINGS_KEY);
    const saved = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    cfg = {
      baseUrl: String(saved.apiBaseUrl || DEFAULT_BASE).trim() || DEFAULT_BASE,
      apiKey: String(saved.apiKey || DEFAULT_API_KEY).trim() || DEFAULT_API_KEY,
      deviceId,
    };
  } catch {
    /* defaults */
  }

  cachedApi = cfg;
  cacheAt = now;
  return cfg;
}

export async function pushPanelOrderNow(
  items?: FingerprintRankPayload[],
): Promise<{
  ok: boolean;
  count: number;
  error?: string;
  ms?: number;
  source?: string;
}> {
  const started = Date.now();
  try {
    const cfg = await loadApiConfig();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10_000);
    try {
      const res = await syncPanelRanks(cfg, controller.signal, items);
      return {
        ok: !!res.ok,
        count: Number(res.count) || 0,
        ms: Date.now() - started,
        source: res.source,
      };
    } finally {
      clearTimeout(timer);
    }
  } catch (e) {
    const aborted =
      (e instanceof DOMException && e.name === 'AbortError') ||
      (e instanceof Error && /abort/i.test(e.message));
    return {
      ok: false,
      count: 0,
      ms: Date.now() - started,
      error: aborted
        ? 'زمان پاسخ پنل تمام شد'
        : e instanceof Error
          ? e.message
          : 'sync failed',
    };
  }
}
