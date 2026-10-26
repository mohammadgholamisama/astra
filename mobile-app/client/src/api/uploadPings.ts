import AsyncStorage from '@react-native-async-storage/async-storage';
import { buildPingSamples, DEFAULT_BASE, submitPingBatch, type ApiConfig } from './client';
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
    const baseUrl = String(saved.apiBaseUrl || DEFAULT_BASE).trim();
    const apiKey = String(saved.apiKey || '').trim();
    if (!apiKey) {
      // Dev default — override via settings.apiKey in storage when deploying
      return {
        baseUrl,
        apiKey: 'dev-local-key',
        deviceId: await getDeviceId(),
      };
    }
    return {
      baseUrl,
      apiKey,
      deviceId: await getDeviceId(),
    };
  } catch {
    return null;
  }
}

/** Push this device's latest live pings to the hub (aggregated server-side for 10m). */
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
