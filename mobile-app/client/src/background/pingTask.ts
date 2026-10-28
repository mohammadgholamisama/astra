import { AppState } from 'react-native';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { runSpeedQueue } from '../utils/speedQueue';
import { runPacketQueue } from '../utils/packetQueue';
import { rankLocally } from '../utils/localRank';
import { buildFingerprintRanksFromPage } from '../utils/fingerprintRank';
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
import {
  testFlagsFromSettings,
  testFlagsToLegacyMode,
} from '../utils/testFlags';
import { isDeviceVpnActive, watchDeviceVpn } from '../native/VpnGuard';
import { cancelAllPings } from '../native/XrayPing';
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
  const flags = testFlagsFromSettings(saved);

  // App open → store owns the cycle (UI + same upload→panel sync)
  if (AppState.currentState === 'active') {
    const store = useAppStore.getState();
    if (store.hydrated && !store.testing) {
      await store.runCycle(false);
    }
    return;
  }

  if (await isDeviceVpnActive()) {
    await saveSettings({
      lastBackgroundStatus: 'رد شد — وی‌پی‌ان روشن است',
      nextRunAt: Date.now() + cycleMs,
      cycleIntervalMs: cycleMs,
    });
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

  const ac = new AbortController();
  const stopVpnWatch = watchDeviceVpn((active) => {
    if (!active || ac.signal.aborted) return;
    ac.abort();
    void cancelAllPings();
  });

  const live = new Map<string, LocalPingResult>();
  let results: LocalPingResult[] = [];

  try {
  // Order: packet → speed → ping
  if (flags.packet && !ac.signal.aborted) {
    results = await runPacketQueue(configs, results, {
      concurrency: 2,
      probes: 10,
      perProbeTimeoutMs: 1500,
      scope: 'all',
      signal: ac.signal,
      onProgress: (_d, _t, latest) => {
        live.set(latest.configHash, latest);
      },
    });
    results.forEach((r) => live.set(r.configHash, r));
  }

  if (flags.speed && !ac.signal.aborted) {
    const packetOk = flags.packet
      ? results.filter(
          (r) =>
            r.packetLossPct != null &&
            r.packetLossPct >= 0 &&
            r.packetLossPct < 100,
        )
      : null;
    const speedConfigs =
      packetOk && packetOk.length > 0
        ? configs.filter((c) =>
            packetOk.some((r) => r.configHash === c.configHash),
          )
        : configs;
    const speedResults = await runSpeedQueue(speedConfigs, results, {
      concurrency: 2,
      downloadBudgetMs: 1600,
      uploadBudgetMs: 1400,
      scope: 'all',
      signal: ac.signal,
      onProgress: (_d, _t, latest) => {
        live.set(latest.configHash, latest);
      },
    });
    const mergedSpeed = new Map(results.map((r) => [r.configHash, r]));
    for (const r of speedResults) mergedSpeed.set(r.configHash, r);
    results = configs.map(
      (c) =>
        mergedSpeed.get(c.configHash) ?? {
          configHash: c.configHash,
          pingMs: 0,
          measuredAt: Date.now(),
        },
    );
    results.forEach((r) => live.set(r.configHash, r));
  }

  if (flags.ping && !ac.signal.aborted) {
    results = await runPingQueue(configs, {
      concurrency: 5,
      timeoutMs: 12_000,
      retries: 1,
      signal: ac.signal,
      onProgress: (_d, _t, latest) => {
        const prev = live.get(latest.configHash);
        live.set(
          latest.configHash,
          prev
            ? { ...prev, pingMs: latest.pingMs, measuredAt: latest.measuredAt }
            : latest,
        );
      },
    });
    results = results.map((r) => {
      const prev = live.get(r.configHash);
      const merged = prev
        ? { ...prev, pingMs: r.pingMs, measuredAt: r.measuredAt }
        : r;
      live.set(r.configHash, merged);
      return merged;
    });
  }

  if (ac.signal.aborted) {
    await saveSettings({
      lastBackgroundStatus: 'قطع شد — وی‌پی‌ان روشن شد',
      nextRunAt: Date.now() + cycleMs,
      cycleIntervalMs: cycleMs,
    });
    return;
  }

  if (results.length === 0) {
    results = [...live.values()];
  } else {
    const byH = new Map(results.map((r) => [r.configHash, r]));
    for (const [h, r] of live) byH.set(h, r);
    results = configs.map(
      (c) =>
        byH.get(c.configHash) ?? {
          configHash: c.configHash,
          pingMs: -1,
          measuredAt: Date.now(),
        },
    );
  }

  const ranked: RankedConfig[] = rankLocally(configs, live);
  const now = Date.now();
  const pingOk = results.filter((r) => r.pingMs > 0).length;
  const speedOk = results.filter(
    (r) => (r.downloadKbps ?? 0) > 0 || (r.uploadKbps ?? 0) > 0,
  ).length;
  const packetOkN = results.filter(
    (r) => r.packetLossPct != null && r.packetLossPct >= 0,
  ).length;
  const parts: string[] = [];
  if (flags.packet) parts.push(`${packetOkN} پکت`);
  if (flags.speed) parts.push(`${speedOk} سرعت`);
  if (flags.ping) parts.push(`${pingOk} پینگ`);
  const summary = `پس‌زمینه · ${parts.join(' · ') || configs.length}`;
  const nextRunAt = now + cycleMs;
  const legacyMode = testFlagsToLegacyMode(flags);

  await saveSnapshot(configs, ranked, {
    operator,
    subUrl: saved.subUrl,
    backgroundEnabled: true,
    cycleIntervalMs: cycleMs,
    nextRunAt,
    testFlags: flags,
    testMode: legacyMode,
    speedRankEnabled: flags.speed || flags.packet,
    lastBackgroundStatus: summary,
  });
  await saveSettings({
    nextRunAt,
    cycleIntervalMs: cycleMs,
    testFlags: flags,
    testMode: legacyMode,
    lastBackgroundStatus: summary,
  });

  await uploadCyclePings(operator, configs, results);
  await new Promise<void>((r) => setTimeout(r, 300));
  const items = buildFingerprintRanksFromPage(ranked);
  // Live successes only — never empty hub-window sync (avoids auto-hide)
  const sync =
    items.length > 0
      ? await pushPanelOrderNow(items)
      : { ok: true, count: 0 };
  const panelHint =
    sync.ok && sync.count > 0
      ? ` · پنل ${sync.count}`
      : sync.ok
        ? ' · پینگ ارسال شد'
        : ' · پنل ناموفق';
  await saveSettings({
    lastBackgroundStatus: `${summary}${panelHint}`,
  });
  } catch (e) {
    const aborted =
      ac.signal.aborted ||
      (e instanceof DOMException && e.name === 'AbortError') ||
      (e instanceof Error && /abort/i.test(e.message));
    if (aborted) {
      await saveSettings({
        lastBackgroundStatus: 'قطع شد — وی‌پی‌ان روشن شد',
        nextRunAt: Date.now() + cycleMs,
        cycleIntervalMs: cycleMs,
      });
    }
  } finally {
    stopVpnWatch();
  }
}
