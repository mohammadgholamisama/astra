import AsyncStorage from '@react-native-async-storage/async-storage';
import type { Operator, ParsedConfig, RankedConfig } from '../types';

const SETTINGS_KEY = 'v2ray_ping_settings_v1';
const CONFIGS_KEY = 'v2ray_ping_configs_v1';
const RANKED_KEY = 'v2ray_ping_ranked_v1';

export type SavedSettings = {
  operator?: Operator;
  subUrl?: string;
  nextRunAt?: number;
  /** Auto-test interval in ms (2m / 5m / 10m / 15m / 30m). */
  cycleIntervalMs?: number;
  backgroundEnabled?: boolean;
  /** Ping first, then download/upload speed on alive configs. */
  speedRankEnabled?: boolean;
  lastBackgroundStatus?: string;
  permissionsAsked?: boolean;
};

export async function loadSettings(): Promise<SavedSettings> {
  try {
    const raw = await AsyncStorage.getItem(SETTINGS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as SavedSettings & {
      configs?: ParsedConfig[];
      ranked?: RankedConfig[];
    };
    // Migrate legacy blob (configs/ranked jammed into settings → quota wipe)
    if (
      (Array.isArray(parsed.configs) && parsed.configs.length > 0) ||
      (Array.isArray(parsed.ranked) && parsed.ranked.length > 0)
    ) {
      const { configs, ranked, ...settingsOnly } = parsed;
      if (configs?.length) await saveConfigs(configs);
      if (ranked?.length) await saveRanked(ranked);
      await AsyncStorage.setItem(SETTINGS_KEY, JSON.stringify(settingsOnly));
      return settingsOnly;
    }
    return parsed;
  } catch {
    return {};
  }
}

export async function saveSettings(partial: SavedSettings): Promise<void> {
  try {
    const prev = await loadSettings();
    await AsyncStorage.setItem(
      SETTINGS_KEY,
      JSON.stringify({ ...prev, ...partial }),
    );
  } catch (e) {
    console.warn('saveSettings failed', e);
  }
}

export async function loadConfigs(): Promise<ParsedConfig[]> {
  try {
    const raw = await AsyncStorage.getItem(CONFIGS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ParsedConfig[];
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    console.warn('loadConfigs failed', e);
    return [];
  }
}

export async function saveConfigs(configs: ParsedConfig[]): Promise<void> {
  try {
    await AsyncStorage.setItem(CONFIGS_KEY, JSON.stringify(configs));
  } catch (e) {
    console.warn('saveConfigs failed', e);
    // Retry without ultra-long remark noise — keep raw (needed for ping)
    try {
      const slim = configs.map((c) => ({
        ...c,
        remark: (c.remark || '').slice(0, 120),
      }));
      await AsyncStorage.setItem(CONFIGS_KEY, JSON.stringify(slim));
    } catch (e2) {
      console.warn('saveConfigs slim failed', e2);
    }
  }
}

export async function loadRanked(): Promise<RankedConfig[]> {
  try {
    const raw = await AsyncStorage.getItem(RANKED_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as RankedConfig[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export async function saveRanked(ranked: RankedConfig[]): Promise<void> {
  try {
    await AsyncStorage.setItem(RANKED_KEY, JSON.stringify(ranked));
  } catch (e) {
    console.warn('saveRanked failed', e);
  }
}

/** Persist last tested snapshot (configs + ranked order together). */
export async function saveSnapshot(
  configs: ParsedConfig[],
  ranked: RankedConfig[],
  extra?: SavedSettings,
): Promise<void> {
  await Promise.all([
    saveConfigs(configs),
    saveRanked(ranked),
    extra ? saveSettings(extra) : Promise.resolve(),
  ]);
}
