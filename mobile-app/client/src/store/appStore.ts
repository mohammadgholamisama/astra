import { create } from 'zustand';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { runSpeedQueue } from '../utils/speedQueue';
import { runPacketQueue } from '../utils/packetQueue';
import { configsAsPending, rankLocally } from '../utils/localRank';
import {
  ensureBackgroundPermissions,
  startBackgroundMonitor,
  stopBackgroundMonitor,
} from '../native/BackgroundMonitor';
import { uploadCyclePings } from '../api/uploadPings';
import { pushPanelOrderNow } from '../api/syncPanel';
import { buildFingerprintRanksFromPage } from '../utils/fingerprintRank';
import { cancelAllPings } from '../native/XrayPing';
import {
  DEFAULT_CYCLE_MS,
  normalizeCycleMs,
  cycleIntervalLabel,
} from '../utils/cycleInterval';
import { isDeviceVpnActive, watchDeviceVpn } from '../native/VpnGuard';
import {
  DEFAULT_TEST_FLAGS,
  testFlagsFromSettings,
  testFlagsHint,
  testFlagsToLegacyMode,
  toggleTestFlag as flipTestFlag,
  type TestFlags,
} from '../utils/testFlags';
import {
  loadConfigs,
  loadRanked,
  loadSettings,
  saveSettings,
  saveSnapshot,
} from './persist';
import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';
import { DEFAULT_OPERATOR } from '../types';

interface AppState {
  subUrl: string;
  configs: ParsedConfig[];
  ranked: RankedConfig[];
  nextRunAt: number | null;
  remainingMs: number;
  cycleIntervalMs: number;
  testing: boolean;
  testingHashes: string[];
  progressDone: number;
  progressTotal: number;
  status: string;
  error: string | null;
  hydrated: boolean;
  backgroundEnabled: boolean;
  /** Multi-select: packet / speed / ping */
  testFlags: TestFlags;
  syncingPanel: boolean;
  /** Short result line under «اعمال ترتیب» */
  panelSyncHint: string;
  permissionsReady: boolean;

  hydrate: () => Promise<void>;
  setSubUrl: (url: string) => void;
  setCycleInterval: (ms: number) => Promise<void>;
  setBackgroundEnabled: (on: boolean) => Promise<void>;
  toggleTestFlag: (id: keyof TestFlags) => Promise<void>;
  tick: () => void;
  loadSub: () => Promise<void>;
  runCycle: (manual?: boolean) => Promise<void>;
  cancelCycle: (reason?: string) => void;
  syncPanelOrder: () => Promise<void>;
}

let cycleAbort: AbortController | null = null;
let cycleStarting = false;
/** After cancel, block accidental re-start from the same finger lift. */
let cancelCooldownUntil = 0;

function intervalOf(get: () => AppState): number {
  return normalizeCycleMs(get().cycleIntervalMs);
}

async function armTimer(
  set: (p: Partial<AppState>) => void,
  get: () => AppState,
) {
  const ms = intervalOf(get);
  // Auto-cycle only while background monitor is on
  if (!get().backgroundEnabled) {
    set({ nextRunAt: null, remainingMs: ms });
    await saveSettings({ nextRunAt: undefined, cycleIntervalMs: ms });
    return null;
  }
  const nextRunAt = Date.now() + ms;
  set({ nextRunAt, remainingMs: ms });
  await saveSettings({ nextRunAt, cycleIntervalMs: ms });
  return nextRunAt;
}

function clearPanelSyncBusy(
  set: (p: Partial<AppState>) => void,
  patch: Partial<AppState> = {},
) {
  set({ syncingPanel: false, ...patch });
}

/** Upload pings, then apply THIS test's ranks to the panel (not 15m window). */
async function uploadThenSyncPanel(
  configs: ParsedConfig[],
  results: LocalPingResult[],
  set: (p: Partial<AppState>) => void,
  get: () => AppState,
  baseStatus: string,
): Promise<void> {
  // Don't stomp a manual "اعمال ترتیب" already in flight
  if (get().syncingPanel) {
    void uploadCyclePings(DEFAULT_OPERATOR, configs, results);
    return;
  }

  set({
    syncingPanel: true,
    panelSyncHint: 'ارسال و اعمال روی پنل…',
  });

  try {
    const sent = await uploadCyclePings(DEFAULT_OPERATOR, configs, results);
    if (get().testing) {
      clearPanelSyncBusy(set);
      return;
    }
    if (sent && get().status.startsWith('تمام')) {
      set({ status: `${baseStatus} · ارسال شد` });
    }

    // Live page ranks — never wait on 15m hub average (that caused «ناکافی»)
    const liveRanked = rankLocally(configs, results);
    const items = buildFingerprintRanksFromPage(liveRanked);

    set({
      status:
        items.length > 0
          ? `اعمال ترتیب پنل · ${items.length} دامنه…`
          : 'اعمال ترتیب پنل…',
      panelSyncHint:
        items.length > 0 ? `اعمال · ${items.length} دامنه…` : 'اعمال روی پنل…',
    });

    await new Promise<void>((r) => setTimeout(r, 200));
    // Live successes → hub merges with 1h window → panel reorder (never hide)
    let res =
      items.length > 0
        ? await pushPanelOrderNow(items)
        : { ok: true, count: 0 as number, source: 'client' as const };

    if (get().testing) {
      clearPanelSyncBusy(set, { panelSyncHint: '' });
      return;
    }

    if (!res.ok) {
      clearPanelSyncBusy(set, {
        panelSyncHint: res.error || 'اعمال ناموفق',
        status: `${baseStatus} · پنل: ${res.error || 'ناموفق'}`,
      });
      return;
    }
    if (res.count === 0) {
      // Soft: pings were still uploaded; avoid scary «منطبق نبود»
      clearPanelSyncBusy(set, {
        panelSyncHint: sent ? 'پینگ ارسال شد' : 'منتظر همگام‌سازی',
        status: sent
          ? `${baseStatus} · پینگ ارسال شد`
          : `${baseStatus} · همگام‌سازی بعداً`,
      });
      return;
    }
    const msHint = res.ms != null ? ` · ${Math.round(res.ms)}ms` : '';
    clearPanelSyncBusy(set, {
      panelSyncHint: `اعمال شد · ${res.count} دامنه`,
      status: `${baseStatus} · پنل ${res.count} دامنه${msHint}`,
    });
  } catch (e) {
    clearPanelSyncBusy(set, {
      panelSyncHint: e instanceof Error ? e.message : 'خطا',
      status: `${baseStatus} · خطا در اعمال پنل`,
    });
  }
}

export const useAppStore = create<AppState>((set, get) => ({
  subUrl: '',
  configs: [],
  ranked: [],
  nextRunAt: null,
  remainingMs: DEFAULT_CYCLE_MS,
  cycleIntervalMs: DEFAULT_CYCLE_MS,
  testing: false,
  testingHashes: [],
  progressDone: 0,
  progressTotal: 0,
  status: 'آماده — ساب را وارد کنید',
  error: null,
  hydrated: false,
  backgroundEnabled: false,
  testFlags: { ...DEFAULT_TEST_FLAGS },
  syncingPanel: false,
  panelSyncHint: '',
  permissionsReady: false,

  hydrate: async () => {
    // Never resume mid-test — leftover native cores cause black screens
    cycleAbort?.abort();
    cycleAbort = null;
    cycleStarting = false;
    cancelAllPings();

    try {
      const [saved, configs, ranked] = await Promise.all([
        loadSettings(),
        loadConfigs(),
        loadRanked(),
      ]);

      const cycleIntervalMs = normalizeCycleMs(saved.cycleIntervalMs);
      const nextRunAt = saved.nextRunAt ?? null;
      const hasCache = configs.length > 0;
      const overdue =
        !!nextRunAt && nextRunAt <= Date.now() && hasCache;

      set({
        subUrl: saved.subUrl ?? '',
        configs,
        ranked:
          ranked.length > 0
            ? ranked
            : hasCache
              ? configsAsPending(configs)
              : [],
        cycleIntervalMs,
        nextRunAt,
        remainingMs: nextRunAt
          ? Math.max(0, nextRunAt - Date.now())
          : cycleIntervalMs,
        status: hasCache
          ? saved.lastBackgroundStatus ?? `${configs.length} کانفیگ از حافظه`
          : saved.lastBackgroundStatus ?? 'آماده',
        testing: false,
        testingHashes: [],
        syncingPanel: false,
        panelSyncHint: '',
        error: null,
        hydrated: true,
        permissionsReady: true,
        backgroundEnabled: saved.backgroundEnabled === true,
        testFlags: testFlagsFromSettings(saved),
      });

      void saveSettings({
        operator: saved.operator ?? DEFAULT_OPERATOR,
        cycleIntervalMs,
        testFlags: testFlagsFromSettings(saved),
        testMode: testFlagsToLegacyMode(testFlagsFromSettings(saved)),
      });

      setTimeout(() => {
        void (async () => {
          try {
            const perms = await ensureBackgroundPermissions();
            await saveSettings({ permissionsAsked: true });
            const wantBg = perms.ok && saved.backgroundEnabled !== false;
            if (perms.ok) {
              set({ backgroundEnabled: wantBg, error: null });
              await saveSettings({ backgroundEnabled: wantBg });
              if (wantBg && (saved.subUrl || configs.length > 0)) {
                void startBackgroundMonitor(cycleIntervalMs, {
                  skipPermissionGate: true,
                });
              }
            } else {
              set({
                backgroundEnabled: false,
                error: perms.message ?? null,
              });
              await saveSettings({ backgroundEnabled: false });
            }

            // Auto-cycle only when background monitor is on
            if (wantBg && overdue && !get().testing) {
              void get().runCycle(false);
            } else if (wantBg && !nextRunAt && hasCache && !get().testing) {
              await armTimer(set, get);
            } else if (!wantBg) {
              set({ nextRunAt: null, remainingMs: cycleIntervalMs });
              await saveSettings({ nextRunAt: undefined });
            }
          } catch {
            /* keep UI up */
          }
        })();
      }, 400);
    } catch {
      set({
        hydrated: true,
        permissionsReady: true,
        backgroundEnabled: false,
        testing: false,
        testingHashes: [],
      });
    }
  },

  setSubUrl: (url) => {
    set({ subUrl: url });
    void saveSettings({ subUrl: url });
  },

  setCycleInterval: async (ms) => {
    const cycleIntervalMs = normalizeCycleMs(ms);
    const prev = get().cycleIntervalMs;
    set({ cycleIntervalMs });
    await saveSettings({ cycleIntervalMs });

    if (get().backgroundEnabled) {
      void startBackgroundMonitor(cycleIntervalMs, {
        skipPermissionGate: true,
      });
    }

    if (!get().testing) {
      await armTimer(set, get); // no-ops timer when background is off
    }

    if (prev !== cycleIntervalMs) {
      const label = cycleIntervalLabel(cycleIntervalMs);
      set({
        status: get().backgroundEnabled
          ? `چرخه هر ${label} تنظیم شد`
          : `بازه ${label} — برای چرخش خودکار پایش را روشن کن`,
      });
    }
  },

  toggleTestFlag: async (id) => {
    const flags = flipTestFlag(get().testFlags, id);
    set({
      testFlags: flags,
      status: testFlagsHint(flags),
    });
    await saveSettings({
      testFlags: flags,
      testMode: testFlagsToLegacyMode(flags),
      speedRankEnabled: flags.speed || flags.packet,
    });
  },

  setBackgroundEnabled: async (on) => {
    if (on) {
      const perms = await ensureBackgroundPermissions();
      if (!perms.ok) {
        set({
          backgroundEnabled: false,
          error: perms.message ?? 'دسترسی لازم برای پایش پس‌زمینه داده نشد',
          status: 'پایش پس‌زمینه نیاز به اجازه دارد',
        });
        await saveSettings({ backgroundEnabled: false });
        return;
      }

      const ms = intervalOf(get);
      set({ backgroundEnabled: true, error: null });
      await saveSettings({
        backgroundEnabled: true,
        operator: DEFAULT_OPERATOR,
        cycleIntervalMs: ms,
      });

      const { subUrl, configs } = get();
      if (subUrl.trim() || configs.length > 0) {
        const started = await startBackgroundMonitor(ms, {
          skipPermissionGate: true,
        });
        await armTimer(set, get);
        set({
          status: started
            ? perms.batteryUnrestricted
              ? `پایش پس‌زمینه فعال · هر ${cycleIntervalLabel(ms)}`
              : 'پایش فعال شد · محدودیت باتری را هم بردارید'
            : 'سرویس پس‌زمینه شروع نشد',
        });
      } else {
        set({ status: 'دسترسی OK — اول لینک ساب را بگذار' });
      }
    } else {
      await stopBackgroundMonitor();
      const ms = intervalOf(get);
      set({
        backgroundEnabled: false,
        status: 'پایش پس‌زمینه خاموش شد',
        nextRunAt: null,
        remainingMs: ms,
      });
      await saveSettings({ backgroundEnabled: false, nextRunAt: undefined });
    }
  },

  tick: () => {
    const { nextRunAt, testing, backgroundEnabled } = get();
    // No auto-cycle while background monitor is off — manual test only
    if (!backgroundEnabled) {
      if (nextRunAt != null) {
        set({ nextRunAt: null, remainingMs: intervalOf(get) });
      }
      return;
    }
    if (!nextRunAt || testing) return;
    const remaining = Math.max(0, nextRunAt - Date.now());
    set({ remainingMs: remaining });
    if (remaining === 0) {
      void get().runCycle(false);
    }
  },

  loadSub: async () => {
    const { subUrl } = get();
    if (!subUrl.trim()) {
      set({ error: 'لینک ساب‌اسکریپشن را وارد کنید' });
      return;
    }
    set({ status: 'در حال دریافت ساب…', error: null });
    try {
      const configs = await loadConfigsFromSubUrl(subUrl.trim(), {
        onStatus: (msg) => set({ status: msg }),
      });
      if (configs.length === 0) {
        set({
          error: 'هیچ کانفیگ معتبری در ساب پیدا نشد',
          status: 'خطا',
        });
        return;
      }
      const ranked = configsAsPending(configs);
      set({
        configs,
        ranked,
        status: `${configs.length} کانفیگ آماده تست`,
        error: null,
      });
      await saveSnapshot(configs, ranked, {
        subUrl: subUrl.trim(),
        operator: DEFAULT_OPERATOR,
        cycleIntervalMs: intervalOf(get),
      });

      if (get().backgroundEnabled) {
        await startBackgroundMonitor(intervalOf(get), {
          skipPermissionGate: true,
        });
        await armTimer(set, get);
      }
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : 'خطا در دریافت ساب',
        status: 'خطا',
      });
    }
  },

  cancelCycle: (reason) => {
    // Idempotent — ignore spam taps / onPressIn+onPress doubles
    if (!get().testing && !cycleStarting && !cycleAbort) return;
    cancelCooldownUntil = Date.now() + 750;
    const status = reason?.trim() || 'تست متوقف شد';
    // Flip UI FIRST so the button feels instant even under load
    set({
      testing: false,
      testingHashes: [],
      syncingPanel: false,
      status,
      error: reason?.includes('وی‌پی‌ان') ? reason : get().error,
    });
    cycleAbort?.abort();
    cycleAbort = null;
    cycleStarting = false;
    cancelAllPings(); // sync fire-and-forget — never await
    const { configs, ranked } = get();
    if (configs.length > 0) {
      setTimeout(() => {
        void saveSnapshot(configs, ranked, {
          lastBackgroundStatus: `${status} — آخرین ترتیب ذخیره شد`,
        });
        void armTimer(set, get);
      }, 0);
    }
  },

  runCycle: async (manual = false) => {
    const state = get();
    if (Date.now() < cancelCooldownUntil) return;
    if (state.testing || cycleStarting) return;
    if (!state.subUrl.trim() && state.configs.length === 0) {
      set({ error: 'لینک ساب را وارد کنید' });
      return;
    }

    // Auto tick / headless: skip if timer was already re-armed (dedupe FG+BG)
    if (!manual) {
      const next = state.nextRunAt;
      if (next != null && next > Date.now() + 1500) return;
    }

    // VPN must be off — otherwise Real Delay measures the tunnel, not the line
    if (await isDeviceVpnActive()) {
      if (manual) {
        set({
          error: 'وی‌پی‌ان دستگاه روشن است — برای تست خاموشش کن',
          status: 'وی‌پی‌ان فعال',
        });
      } else {
        set({ status: 'چرخه رد شد — وی‌پی‌ان روشن است' });
        void armTimer(set, get);
      }
      return;
    }

    cycleStarting = true;
    cycleAbort?.abort();
    cancelAllPings();
    const ac = new AbortController();
    cycleAbort = ac;

    const stopVpnWatch = watchDeviceVpn((active) => {
      if (!active) return;
      if (!get().testing && !cycleStarting) return;
      get().cancelCycle('وی‌پی‌ان روشن شد — تست قطع شد');
    });

    set({
      testing: true,
      testingHashes: [],
      error: null,
      status: manual ? 'شروع تست…' : 'چرخه خودکار…',
      progressDone: 0,
      progressTotal: state.configs.length || 0,
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    try {
      if (ac.signal.aborted || !get().testing) return;

      let configs = state.configs;
      if (configs.length === 0) {
        if (!state.subUrl.trim()) throw new Error('ساب خالی است');
        set({ status: 'دریافت ساب…' });
        configs = await loadConfigsFromSubUrl(state.subUrl.trim(), {
          signal: ac.signal,
          onStatus: (msg) => {
            if (!ac.signal.aborted && get().testing) set({ status: msg });
          },
        });
        if (ac.signal.aborted || !get().testing) return;
        const pending = configsAsPending(configs);
        set({ configs, ranked: pending, progressTotal: configs.length });
        void saveSnapshot(configs, pending);
      }
      if (configs.length === 0) {
        throw new Error('ساب خالی است');
      }

      const flags = get().testFlags;
      const live = new Map<string, LocalPingResult>();
      const inFlight = new Set<string>();
      const startHint = flags.packet
        ? 'پکت…'
        : flags.speed
          ? 'سرعت…'
          : 'پینگ…';
      set({
        progressTotal: configs.length,
        status: startHint,
      });

      let lastLight = 0;
      let lastRank = 0;
      let rankTimer: ReturnType<typeof setTimeout> | null = null;
      let latestDone = 0;
      let latestTotal = configs.length;
      let latestHint = '';

      const flushRank = () => {
        rankTimer = null;
        lastRank = Date.now();
        if (ac.signal.aborted || !get().testing) return;
        set({
          ranked: rankLocally(configs, live, inFlight),
          testingHashes: [...inFlight].slice(0, 6),
          progressDone: latestDone,
          progressTotal: latestTotal,
          status: latestHint || `تست… ${latestDone}/${latestTotal}`,
        });
      };

      const persistAbort = () => {
        setTimeout(() => {
          try {
            const ranked = rankLocally(configs, live);
            set({ ranked, testingHashes: [], testing: false });
            void saveSnapshot(configs, ranked, {
              lastBackgroundStatus: 'تست متوقف شد — آخرین ترتیب ذخیره شد',
              testFlags: get().testFlags,
              testMode: testFlagsToLegacyMode(get().testFlags),
            });
          } catch {
            /* ignore */
          }
        }, 50);
      };

      const onMetricProgress = (
        label: string,
        done: number,
        total: number,
        latest: LocalPingResult,
        detail: string,
      ) => {
        if (ac.signal.aborted || !get().testing) return;
        live.set(latest.configHash, latest);
        const hint = `${label} · ${done}/${total} · ${detail}`;
        const now = Date.now();
        if (done >= total - 1 || now - lastLight >= 180) {
          lastLight = now;
          set({
            progressDone: done,
            progressTotal: total,
            status: hint,
            ranked: rankLocally(configs, live),
          });
        } else if (now - lastRank >= 400) {
          lastRank = now;
          set({ ranked: rankLocally(configs, live) });
        }
      };

      let results: LocalPingResult[] = [];

      // —— 1) Packet loss (first priority) ——
      if (flags.packet && !ac.signal.aborted && get().testing) {
        set({
          status: `پکت · همه ${configs.length}…`,
          progressDone: 0,
          progressTotal: configs.length,
          testingHashes: [],
        });
        lastLight = 0;
        lastRank = 0;
        results = await runPacketQueue(configs, results, {
          concurrency: 2,
          probes: 10,
          perProbeTimeoutMs: 1500,
          scope: 'all',
          signal: ac.signal,
          onProgress: (done, total, latest) => {
            const loss =
              latest.packetLossPct != null && latest.packetLossPct >= 0
                ? `${latest.packetLossPct}٪`
                : '—';
            onMetricProgress('پکت', done, total, latest, loss);
          },
        });
        results.forEach((r) => live.set(r.configHash, r));
        if (ac.signal.aborted || !get().testing) {
          persistAbort();
          return;
        }
        set({ ranked: rankLocally(configs, live), testingHashes: [] });
      }

      // —— 2) Speed ——
      if (flags.speed && !ac.signal.aborted && get().testing) {
        // After packet: only configs that aren't total loss; else all
        const packetFiltered = flags.packet
          ? results.filter(
              (r) =>
                r.packetLossPct != null &&
                r.packetLossPct >= 0 &&
                r.packetLossPct < 100,
            )
          : null;
        const scope: 'all' | 'alive' = 'all';
        const speedConfigs =
          packetFiltered && packetFiltered.length > 0
            ? configs.filter((c) =>
                packetFiltered.some((r) => r.configHash === c.configHash),
              )
            : configs;
        const targetN = speedConfigs.length;
        set({
          status: `سرعت · ${targetN}…`,
          progressDone: 0,
          progressTotal: Math.max(1, targetN),
          testingHashes: [],
        });
        lastLight = 0;
        lastRank = 0;
        const speedResults = await runSpeedQueue(speedConfigs, results, {
          concurrency: 2,
          downloadBudgetMs: 1600,
          uploadBudgetMs: 1400,
          scope,
          signal: ac.signal,
          onProgress: (done, total, latest) => {
            const dl =
              latest.downloadKbps != null && latest.downloadKbps > 0
                ? `دان ${Math.round(latest.downloadKbps)}`
                : 'دان —';
            const ul =
              latest.uploadKbps != null && latest.uploadKbps > 0
                ? `آپ ${Math.round(latest.uploadKbps)}`
                : 'آپ —';
            onMetricProgress('سرعت', done, total, latest, `${dl} · ${ul}`);
          },
        });
        // Merge subset speed rows back into full results
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
        if (ac.signal.aborted || !get().testing) {
          persistAbort();
          return;
        }
        set({ ranked: rankLocally(configs, live), testingHashes: [] });
      }

      // —— 3) Ping (last) ——
      if (flags.ping && !ac.signal.aborted && get().testing) {
        set({
          status: `پینگ · ${configs.length}…`,
          progressDone: 0,
          progressTotal: configs.length,
          testingHashes: [],
        });
        lastLight = 0;
        lastRank = 0;
        results = await runPingQueue(configs, {
          concurrency: 5,
          timeoutMs: 12_000,
          retries: 1,
          signal: ac.signal,
          onStart: (cfg) => {
            if (ac.signal.aborted || !get().testing) return;
            inFlight.add(cfg.configHash);
          },
          onProgress: (done, total, latest) => {
            if (ac.signal.aborted || !get().testing) return;
            inFlight.delete(latest.configHash);
            // Merge with prior packet/speed fields
            const prev = live.get(latest.configHash);
            const merged: LocalPingResult = prev
              ? {
                  ...prev,
                  pingMs: latest.pingMs,
                  measuredAt: latest.measuredAt,
                }
              : latest;
            live.set(latest.configHash, merged);
            latestDone = done;
            latestTotal = total;
            latestHint =
              latest.pingMs > 0
                ? `پینگ · ${done}/${total} · ${Math.round(latest.pingMs)}ms`
                : `پینگ · ${done}/${total} · قطع`;

            const now = Date.now();
            const nearEnd = total > 0 && done >= Math.max(1, total - 3);
            if (nearEnd || now - lastLight >= 200) {
              lastLight = now;
              set({
                progressDone: done,
                progressTotal: total,
                status: latestHint,
              });
            }
            if (now - lastRank >= 450) {
              if (rankTimer) clearTimeout(rankTimer);
              flushRank();
            } else if (!rankTimer) {
              rankTimer = setTimeout(flushRank, 450 - (now - lastRank));
            }
          },
        });

        if (rankTimer) clearTimeout(rankTimer);

        if (ac.signal.aborted || !get().testing) {
          results.forEach((r) => {
            const prev = live.get(r.configHash);
            live.set(
              r.configHash,
              prev
                ? { ...prev, pingMs: r.pingMs, measuredAt: r.measuredAt }
                : r,
            );
          });
          persistAbort();
          return;
        }

        // Merge ping into existing packet/speed rows
        results = results.map((r) => {
          const prev = live.get(r.configHash);
          const merged = prev
            ? { ...prev, pingMs: r.pingMs, measuredAt: r.measuredAt }
            : r;
          live.set(r.configHash, merged);
          return merged;
        });
        set({
          ranked: rankLocally(configs, live),
          testingHashes: [],
          progressDone: configs.length,
          progressTotal: configs.length,
        });
      }

      if (ac.signal.aborted || !get().testing) {
        persistAbort();
        return;
      }

      // Ensure results covers every live row for upload
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

      const ranked = rankLocally(configs, live);
      const pingOk = results.filter((r) => r.pingMs > 0).length;
      const withSpeed = results.filter(
        (r) => (r.downloadKbps ?? 0) > 0 || (r.uploadKbps ?? 0) > 0,
      ).length;
      const withPacket = results.filter(
        (r) => r.packetLossPct != null && r.packetLossPct >= 0,
      ).length;
      const parts: string[] = [];
      if (flags.packet) parts.push(`${withPacket} پکت`);
      if (flags.speed) parts.push(`${withSpeed} سرعت`);
      if (flags.ping) parts.push(`${pingOk} پینگ`);
      const baseStatus = `تمام · ${parts.join(' · ') || configs.length}`;
      set({
        testing: false,
        testingHashes: [],
        ranked,
        status: baseStatus,
        progressDone: configs.length,
        progressTotal: configs.length,
      });
      void armTimer(set, get);
      void saveSnapshot(configs, ranked, {
        lastBackgroundStatus: baseStatus,
        operator: DEFAULT_OPERATOR,
        cycleIntervalMs: intervalOf(get),
        testFlags: flags,
        testMode: testFlagsToLegacyMode(flags),
        speedRankEnabled: flags.speed || flags.packet,
      });

      // Upload → multi-device 15m avg → reorder panel (auto)
      void uploadThenSyncPanel(configs, results, set, get, baseStatus);

      if (get().backgroundEnabled) {
        void startBackgroundMonitor(intervalOf(get), {
          skipPermissionGate: true,
        });
      }
    } catch (e) {
      const aborted =
        (e instanceof DOMException && e.name === 'AbortError') ||
        (e instanceof Error && /abort/i.test(e.message));

      if (!aborted) {
        set({
          testing: false,
          testingHashes: [],
          error: e instanceof Error ? e.message : 'خطای ناشناخته',
          status: 'خطا در چرخه',
        });
      } else {
        set({
          testing: false,
          testingHashes: [],
          status: 'تست متوقف شد',
        });
      }
      void armTimer(set, get);
    } finally {
      stopVpnWatch();
      cycleStarting = false;
      if (cycleAbort === ac) cycleAbort = null;
    }
  },

  syncPanelOrder: async () => {
    if (get().syncingPanel || get().testing) return;

    const items = buildFingerprintRanksFromPage(get().ranked);
    if (items.length === 0) {
      set({
        error: null,
        panelSyncHint: 'اول تست بگیر',
        status: 'اول تست بگیر — فقط پینگ‌های موفق روی پنل اعمال می‌شوند',
      });
      return;
    }

    set({
      syncingPanel: true,
      error: null,
      panelSyncHint: `اعمال · ${items.length} دامنه…`,
      status: `اعمال فوری · ${items.length} دامنه…`,
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    try {
      const { configs, ranked } = get();
      // Await upload so panel sees fresh samples before reorder
      const toUpload = () =>
        ranked
          .filter(
            (r) =>
              r.measured &&
              (r.avg_ping > 0 ||
                (r.download_kbps ?? 0) > 0 ||
                (r.upload_kbps ?? 0) > 0 ||
                (r.packet_loss_pct != null &&
                  r.packet_loss_pct >= 0 &&
                  r.packet_loss_pct < 100) ||
                r.success_rate > 0),
          )
          .map((r) => ({
            configHash: r.config_hash,
            pingMs: r.avg_ping > 0 ? r.avg_ping : 0,
            downloadKbps:
              r.download_kbps != null && r.download_kbps > 0
                ? r.download_kbps
                : undefined,
            uploadKbps:
              r.upload_kbps != null && r.upload_kbps > 0
                ? r.upload_kbps
                : undefined,
            packetLossPct:
              r.packet_loss_pct != null && r.packet_loss_pct >= 0
                ? r.packet_loss_pct
                : undefined,
            measuredAt: Date.now(),
          }));

      await uploadCyclePings(DEFAULT_OPERATOR, configs, toUpload());

      // Live successes → hub merges 1h window → panel order only (no hide)
      const res = await pushPanelOrderNow(items);

      if (!res.ok) {
        clearPanelSyncBusy(set, {
          error: res.error || 'همگام‌سازی پنل ناموفق',
          panelSyncHint: res.error || 'ناموفق — دوباره بزن',
          status: 'خطا در اعمال ترتیب',
        });
        return;
      }
      if (res.count === 0) {
        clearPanelSyncBusy(set, {
          panelSyncHint: 'پینگ ارسال شد',
          status: 'پینگ ارسال شد — همگام‌سازی بعدی خودکار است',
        });
        return;
      }
      const msHint = res.ms != null ? ` · ${Math.round(res.ms)}ms` : '';
      clearPanelSyncBusy(set, {
        panelSyncHint: `اعمال شد · ${res.count} دامنه`,
        status: `ترتیب پنل اعمال شد · ${res.count} دامنه${msHint}`,
      });
    } catch (e) {
      clearPanelSyncBusy(set, {
        error: e instanceof Error ? e.message : 'خطا در اعمال ترتیب',
        panelSyncHint: 'خطا — دوباره بزن',
        status: 'خطا در اعمال ترتیب',
      });
    }
  },
}));

export { DEFAULT_CYCLE_MS as CYCLE_MS };
