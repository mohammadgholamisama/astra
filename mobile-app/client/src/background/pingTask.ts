import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { rankLocally } from '../utils/localRank';
import { uploadCyclePings } from '../api/uploadPings';
import {
  loadConfigs,
  loadSettings,
  saveSettings,
  saveSnapshot,
} from '../store/persist';
import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';
import { DEFAULT_OPERATOR } from '../types';

const CYCLE_MS = 5 * 60 * 1000;

/**
 * Headless background cycle — runs even when UI is not open.
 * Triggered by Android PingMonitorService every 5 minutes.
 */
export default async function backgroundPingTask(): Promise<void> {
  const saved = await loadSettings();
  if (!saved.backgroundEnabled) return;
  if (!saved.subUrl?.trim()) return;

  const operator = saved.operator ?? DEFAULT_OPERATOR;

  let configs: ParsedConfig[] = await loadConfigs();
  if (configs.length === 0) {
    try {
      configs = await loadConfigsFromSubUrl(saved.subUrl.trim());
    } catch {
      return;
    }
  }
  if (configs.length === 0) return;

  const live = new Map<string, LocalPingResult>();
  const results = await runPingQueue(configs, {
    concurrency: 28,
    timeoutMs: 1600,
    tcpTimeoutMs: 280,
    tcpPrecheck: true,
    onProgress: (_d, _t, latest) => {
      live.set(latest.configHash, latest);
    },
  });
  results.forEach((r) => live.set(r.configHash, r));
  const ranked: RankedConfig[] = rankLocally(configs, live);
  const now = Date.now();
  const ok = results.filter((r) => r.pingMs > 0).length;

  await saveSnapshot(configs, ranked, {
    operator,
    subUrl: saved.subUrl,
    backgroundEnabled: true,
    nextRunAt: now + CYCLE_MS,
    lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق`,
  });
  await saveSettings({
    nextRunAt: now + CYCLE_MS,
    lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق`,
  });

  await uploadCyclePings(operator, configs, results);
}
