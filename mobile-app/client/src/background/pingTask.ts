import AsyncStorage from '@react-native-async-storage/async-storage';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { rankLocally } from '../utils/localRank';
import { uploadCyclePings } from '../api/uploadPings';
import type { LocalPingResult, Operator, ParsedConfig, RankedConfig } from '../types';

const STORAGE_KEY = 'v2ray_ping_settings_v1';
const CYCLE_MS = 5 * 60 * 1000;

/**
 * Headless background cycle — runs even when UI is not open.
 * Triggered by Android PingMonitorService every 5 minutes.
 */
export default async function backgroundPingTask(): Promise<void> {
  const raw = await AsyncStorage.getItem(STORAGE_KEY);
  if (!raw) return;
  const saved = JSON.parse(raw) as {
    operator?: Operator;
    subUrl?: string;
    backgroundEnabled?: boolean;
  };
  if (!saved.backgroundEnabled) return;
  if (!saved.operator || !saved.subUrl?.trim()) return;

  let configs: ParsedConfig[] = [];
  try {
    configs = await loadConfigsFromSubUrl(saved.subUrl.trim());
  } catch {
    return;
  }
  if (configs.length === 0) return;

  const live = new Map<string, LocalPingResult>();
  const results = await runPingQueue(configs, {
    concurrency: 3,
    timeoutMs: 6500,
    tcpPrecheck: true,
    onProgress: (_d, _t, latest) => {
      live.set(latest.configHash, latest);
    },
  });
  results.forEach((r) => live.set(r.configHash, r));
  const ranked: RankedConfig[] = rankLocally(saved.operator, configs, live);
  const now = Date.now();
  const ok = results.filter((r) => r.pingMs > 0).length;

  const prev = JSON.parse((await AsyncStorage.getItem(STORAGE_KEY)) || '{}') as Record<
    string,
    unknown
  >;
  await AsyncStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      ...prev,
      operator: saved.operator,
      subUrl: saved.subUrl,
      backgroundEnabled: true,
      configs,
      ranked,
      nextRunAt: now + CYCLE_MS,
      lastBackgroundAt: now,
      lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق`,
    }),
  );

  await uploadCyclePings(saved.operator, configs, results);
}
