import { AppState } from 'react-native';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { rankLocally } from '../utils/localRank';
import { uploadCyclePings } from '../api/uploadPings';
import { pushPanelOrderNow } from '../api/syncPanel';
import {
  loadConfigs,
  loadSettings,
  saveSettings,
  saveSnapshot,
} from '../store/persist';
import { useAppStore } from '../store/appStore';
import {
  DEFAULT_CYCLE_MS,
  normalizeCycleMs,
} from '../utils/cycleInterval';
import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';
import { DEFAULT_OPERATOR } from '../types';

/**
 * Headless background cycle — runs even when UI is not open.
 * When the app is in the foreground, delegates to the Zustand store
 * so the UI + panel sync path stay in sync.
 */
export default async function backgroundPingTask(): Promise<void> {
  const saved = await loadSettings();
  if (!saved.backgroundEnabled) return;
  if (!saved.subUrl?.trim() && !(await loadConfigs()).length) return;

  const cycleMs = normalizeCycleMs(saved.cycleIntervalMs ?? DEFAULT_CYCLE_MS);

  // App open → store owns the cycle (UI + same upload→15m panel sync)
  if (AppState.currentState === 'active') {
    const store = useAppStore.getState();
    if (store.hydrated && !store.testing) {
      await store.runCycle(false);
    }
    return;
  }

  const operator = saved.operator ?? DEFAULT_OPERATOR;

  let configs: ParsedConfig[] = await loadConfigs();
  if (configs.length === 0) {
    try {
      configs = await loadConfigsFromSubUrl(saved.subUrl!.trim());
    } catch {
      return;
    }
  }
  if (configs.length === 0) return;

  const live = new Map<string, LocalPingResult>();
  let results = await runPingQueue(configs, {
    concurrency: 5,
    timeoutMs: 2200,
    tcpTimeoutMs: 600,
    tcpPrecheck: 'soft',
    retries: 1,
    onProgress: (_d, _t, latest) => {
      live.set(latest.configHash, latest);
    },
  });
  results.forEach((r) => live.set(r.configHash, r));

  if (saved.speedRankEnabled) {
    const { runSpeedQueue } = await import('../utils/speedQueue');
    results = await runSpeedQueue(configs, results, {
      concurrency: 2,
      downloadBudgetMs: 1600,
      uploadBudgetMs: 1400,
      onProgress: (_d, _t, latest) => {
        live.set(latest.configHash, latest);
      },
    });
    results.forEach((r) => live.set(r.configHash, r));
  }

  const ranked: RankedConfig[] = rankLocally(configs, live);
  const now = Date.now();
  const ok = results.filter((r) => r.pingMs > 0).length;
  const nextRunAt = now + cycleMs;

  await saveSnapshot(configs, ranked, {
    operator,
    subUrl: saved.subUrl,
    backgroundEnabled: true,
    cycleIntervalMs: cycleMs,
    nextRunAt,
    lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق`,
  });
  await saveSettings({
    nextRunAt,
    cycleIntervalMs: cycleMs,
    lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق`,
  });

  // Push samples then reorder panel from multi-device 15-minute window
  await uploadCyclePings(operator, configs, results);
  await new Promise<void>((r) => setTimeout(r, 500));
  const sync = await pushPanelOrderNow();
  const panelHint =
    sync.ok && sync.count > 0
      ? ` · پنل ${sync.count}`
      : sync.ok
        ? ' · پنل: میانگین ناکافی'
        : ' · پنل ناموفق';
  await saveSettings({
    lastBackgroundStatus: `پس‌زمینه · ${ok}/${configs.length} موفق${panelHint}`,
  });
}
