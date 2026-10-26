import AsyncStorage from '@react-native-async-storage/async-storage';
import {
  buildPingSamples,
  DEFAULT_API_KEY,
  DEFAULT_BASE,
  submitPingBatch,
  type ApiConfig,
} from './client';
import type { LocalPingResult, Operator, ParsedConfig } from '../types';

const SETTINGS_KEY = 'v2ray_ping_settings_v1';
const DEVICE_KEY = 'v2ray_ping_device_id';

async function getDeviceId(): Promise<string> {
  try {
    const existing = await AsyncStorage.getItem(DEVICE_KEY);
    if (existing) return existing;
    const id = `dev_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    await AsyncStorage.setItem(DEVICE_KEY, id);
    return id;
  } catch {
    return `dev_${Date.now()}`;
  }
}

async function loadApiConfig(): Promise<ApiConfig | null> {
  try {
    const raw = await AsyncStorage.getItem(SETTINGS_KEY);
    const saved = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    const baseUrl = String(saved.apiBaseUrl || DEFAULT_BASE).trim() || DEFAULT_BASE;
    const apiKey = String(saved.apiKey || DEFAULT_API_KEY).trim() || DEFAULT_API_KEY;
    return {
      baseUrl,
      apiKey,
      deviceId: await getDeviceId(),
    };
  } catch {
    return {
      baseUrl: DEFAULT_BASE,
      apiKey: DEFAULT_API_KEY,
      deviceId: await getDeviceId(),
    };
  }
}

/** Push this device's latest live pings to the hub (aggregated server-side for 15m). */
export async function uploadCyclePings(
  operator: Operator,
  configs: ParsedConfig[],
  results: LocalPingResult[],
): Promise<boolean> {
  const cfg = await loadApiConfig();
  if (!cfg) return false;
  const samples = buildPingSamples(operator, configs, results, cfg.deviceId).filter(
    (s) => s.host && s.port,
  );
  if (samples.length === 0) return false;
  try {
    await submitPingBatch(cfg, operator, samples);
    return true;
  } catch {
    return false;
  }
}
