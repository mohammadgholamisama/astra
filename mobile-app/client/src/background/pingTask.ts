import { AppState } from 'react-native';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { runSpeedQueue } from '../utils/speedQueue';
import { runPacketQueue } from '../utils/packetQueue';
import { isCycleAlive, rankLocally } from '../utils/localRank';
import {
  loadUptimeMap,
  recordUptimeCycle,
  saveUptimeMap,
} from '../utils/uptimeHistory';
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
  latencySampleCount,
  latencyTimeoutMs,
  needsLatencyPass,
  testFlagsFromSettings,
  testFlagsToLegacyMode,
} from '../utils/testFlags';
import { STABILITY_PROBE_URLS } from '../utils/stabilityProbes';
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

  // Only block when this app is routed through VPN (excluded apps may test)
  if (await isDeviceVpnActive()) {
    await saveSettings({
      lastBackgroundStatus: 'رد شد — وی‌پی‌ان روی این اپ فعال است',
      nextRunAt: Date.now() + cycleMs,
      cycleIntervalMs: cycleMs,
    });
    return;
  }

  const operator = saved.operator ?? DEFAULT_OPERATOR;

  let configs: ParsedConfig[] = await loadConfigs();
  // Always refresh sub before background test
  if (saved.subUrl?.trim()) {
    try {
      configs = await loadConfigsFromSubUrl(saved.subUrl.trim());
      await saveSnapshot(configs, rankLocally(configs, [], {
        uptimeMap: await loadUptimeMap(),
        useUptime: flags.uptime,
      }));
    } catch {
      if (configs.length === 0) return;
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
  const samples = latencySampleCount(flags);
  const latTimeout = latencyTimeoutMs(flags);

  try {
    // Order: latency → packet → download/upload
    if (needsLatencyPass(flags) && !ac.signal.aborted) {
      results = await runPingQueue(configs, {
        concurrency: 5,
        timeoutMs: latTimeout,
        retries: samples > 1 ? 0 : 1,
        samples,
        probeUrls: flags.stability ? STABILITY_PROBE_URLS : undefined,
        wantJitter: flags.jitter,
        wantStability: flags.stability,
        signal: ac.signal,
        onProgress: (_d, _t, latest) => {
          const prev = live.get(latest.configHash);
          live.set(
            latest.configHash,
            prev
              ? {
                  ...prev,
                  pingMs: latest.pingMs,
                  jitterMs: latest.jitterMs ?? prev.jitterMs,
                  stabilityPct: latest.stabilityPct ?? prev.stabilityPct,
                  measuredAt: latest.measuredAt,
                }
              : latest,
          );
        },
      });
      results = results.map((r) => {
        const prev = live.get(r.configHash);
        const merged = prev
          ? {
              ...prev,
              pingMs: r.pingMs,
              jitterMs: r.jitterMs ?? prev.jitterMs,
              stabilityPct: r.stabilityPct ?? prev.stabilityPct,
              measuredAt: r.measuredAt,
            }
          : r;
        live.set(r.configHash, merged);
        return merged;
      });
    }

    if (flags.packet && !ac.signal.aborted) {
      const alive = needsLatencyPass(flags)
        ? results.filter((r) => r.pingMs > 0)
        : null;
      const packetConfigs =
        alive && alive.length > 0
          ? configs.filter((c) =>
              alive.some((r) => r.configHash === c.configHash),
            )
          : configs;
      const packetResults = await runPacketQueue(packetConfigs, results, {
        concurrency: 2,
        probes: 100,
        perProbeTimeoutMs: 800,
        scope: 'all',
        signal: ac.signal,
        onProgress: (_d, _t, latest) => {
          live.set(latest.configHash, latest);
        },
      });
      const mergedPkt = new Map(results.map((r) => [r.configHash, r]));
      for (const r of packetResults) mergedPkt.set(r.configHash, r);
      results = configs.map(
        (c) =>
          mergedPkt.get(c.configHash) ?? {
            configHash: c.configHash,
            pingMs: -1,
            measuredAt: Date.now(),
          },
      );
      results.forEach((r) => live.set(r.configHash, r));
    }

    if ((flags.download || flags.upload) && !ac.signal.aborted) {
      const filtered = flags.packet
        ? results.filter(
            (r) =>
              r.packetLossPct != null &&
              r.packetLossPct >= 0 &&
              r.packetLossPct < 100,
          )
        : needsLatencyPass(flags)
          ? results.filter((r) => r.pingMs > 0)
          : null;
      const speedConfigs =
        filtered && filtered.length > 0
          ? configs.filter((c) =>
              filtered.some((r) => r.configHash === c.configHash),
            )
          : configs;
      const speedResults = await runSpeedQueue(speedConfigs, results, {
        concurrency: 2,
        downloadBudgetMs: flags.download ? 1600 : 0,
        uploadBudgetMs: flags.upload ? 1400 : 0,
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

    if (ac.signal.aborted) {
      await saveSettings({
        lastBackgroundStatus: 'قطع شد — وی‌پی‌ان روی این اپ فعال شد',
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

    const outcomes = new Map<string, boolean>();
    for (const c of configs) {
      outcomes.set(c.configHash, isCycleAlive(live.get(c.configHash)));
    }
    const keep = new Set(configs.map((c) => c.configHash));
    const prevUp = await loadUptimeMap();
    const nextUp = recordUptimeCycle(prevUp, outcomes, keep);
    await saveUptimeMap(nextUp);

    const ranked: RankedConfig[] = rankLocally(configs, live, {
      uptimeMap: nextUp,
      useUptime: flags.uptime,
    });
    const now = Date.now();
    const pingOk = results.filter((r) => r.pingMs > 0).length;
    const speedOk = results.filter(
      (r) => (r.downloadKbps ?? 0) > 0 || (r.uploadKbps ?? 0) > 0,
    ).length;
    const packetOkN = results.filter(
      (r) => r.packetLossPct != null && r.packetLossPct >= 0,
    ).length;
    const parts: string[] = [];
    if (flags.stability) {
      parts.push(
        `${results.filter((r) => (r.stabilityPct ?? -1) >= 0).length} پایداری`,
      );
    }
    if (flags.packet) parts.push(`${packetOkN} پکت`);
    if (flags.jitter) {
      parts.push(
        `${results.filter((r) => (r.jitterMs ?? -1) >= 0).length} جیتر`,
      );
    }
    if (needsLatencyPass(flags)) parts.push(`${pingOk} پینگ`);
    if (flags.download || flags.upload) parts.push(`${speedOk} سرعت`);
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
      speedRankEnabled:
        flags.download || flags.upload || flags.packet,
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
        lastBackgroundStatus: 'قطع شد — وی‌پی‌ان روی این اپ فعال شد',
        nextRunAt: Date.now() + cycleMs,
        cycleIntervalMs: cycleMs,
      });
    }
  } finally {
    stopVpnWatch();
  }
}
