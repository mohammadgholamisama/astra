import { create } from 'zustand';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
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
  cycleMinutesLabel,
} from '../utils/cycleInterval';
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
  syncingPanel: boolean;
  permissionsReady: boolean;

  hydrate: () => Promise<void>;
  setSubUrl: (url: string) => void;
  setCycleInterval: (ms: number) => Promise<void>;
  setBackgroundEnabled: (on: boolean) => Promise<void>;
  tick: () => void;
  loadSub: () => Promise<void>;
  runCycle: (manual?: boolean) => Promise<void>;
  cancelCycle: () => void;
  syncPanelOrder: () => Promise<void>;
}

let cycleAbort: AbortController | null = null;
let cycleStarting = false;

function intervalOf(get: () => AppState): number {
  return normalizeCycleMs(get().cycleIntervalMs);
}

async function armTimer(
  set: (p: Partial<AppState>) => void,
  get: () => AppState,
) {
  const ms = intervalOf(get);
  const nextRunAt = Date.now() + ms;
  set({ nextRunAt, remainingMs: ms });
  await saveSettings({ nextRunAt, cycleIntervalMs: ms });
  return nextRunAt;
}

/** Upload this device's pings, then reorder panel from multi-device 15m window. */
async function uploadThenSyncPanel(
  configs: ParsedConfig[],
  results: LocalPingResult[],
  set: (p: Partial<AppState>) => void,
  get: () => AppState,
  baseStatus: string,
): Promise<void> {
  const sent = await uploadCyclePings(DEFAULT_OPERATOR, configs, results);
  if (!get().testing && get().status.startsWith('تمام') && sent) {
    set({ status: `${baseStatus} · ارسال شد` });
  }

  set({
    syncingPanel: true,
    status: sent
      ? 'میانگین ۱۵د · اعمال ترتیب پنل…'
      : 'اعمال ترتیب پنل از پنجرهٔ سرور…',
  });

  // Brief yield so Redis ingest is visible to RankedFingerprints
  await new Promise<void>((r) => setTimeout(r, 500));
  const res = await pushPanelOrderNow();

  if (get().testing) {
    set({ syncingPanel: false });
    return;
  }

  if (!res.ok) {
    set({
      syncingPanel: false,
      status: `${baseStatus} · پنل: ${res.error || 'ناموفق'}`,
    });
    return;
  }
  if (res.count === 0) {
    set({
      syncingPanel: false,
      status: `${baseStatus} · هنوز میانگین ۱۵د کافی نیست`,
    });
    return;
  }
  const msHint = res.ms != null ? ` · ${Math.round(res.ms)}ms` : '';
  set({
    syncingPanel: false,
    status: `${baseStatus} · پنل ${res.count} دامنه (میانگین ۱۵د)${msHint}`,
  });
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
  syncingPanel: false,
  permissionsReady: false,

  hydrate: async () => {
    // Never resume mid-test — leftover native cores cause black screens
    cycleAbort?.abort();
    cycleAbort = null;
    cycleStarting = false;
    void cancelAllPings();

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
        error: null,
        hydrated: true,
        permissionsReady: true,
        backgroundEnabled: saved.backgroundEnabled === true,
      });

      void saveSettings({
        operator: saved.operator ?? DEFAULT_OPERATOR,
        cycleIntervalMs,
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

            // Timer already due while app was closed → run now
            if (overdue && !get().testing) {
              void get().runCycle(false);
            } else if (wantBg && !nextRunAt && hasCache && !get().testing) {
              await armTimer(set, get);
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
      await armTimer(set, get);
    }

    if (prev !== cycleIntervalMs) {
      set({
        status: `چرخه هر ${cycleMinutesLabel(cycleIntervalMs)} دقیقه تنظیم شد`,
      });
    }
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
              ? `پایش پس‌زمینه فعال · هر ${cycleMinutesLabel(ms)}د`
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
    const { nextRunAt, testing } = get();
    if (!nextRunAt || testing) return;
    const remaining = Math.max(0, nextRunAt - Date.now());
    set({ remainingMs: remaining });
    // Fire whether or not background service is on — service skips when UI is active
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

  cancelCycle: () => {
    cycleAbort?.abort();
    cycleAbort = null;
    cycleStarting = false;
    void cancelAllPings();
    set({
      testing: false,
      testingHashes: [],
      syncingPanel: false,
      status: 'تست متوقف شد',
    });
    const { configs, ranked } = get();
    if (configs.length > 0 && ranked.length > 0) {
      setTimeout(() => {
        void saveSnapshot(configs, ranked, {
          lastBackgroundStatus: 'تست متوقف شد — آخرین ترتیب ذخیره شد',
        });
        void armTimer(set, get);
      }, 0);
    }
  },

  runCycle: async (manual = false) => {
    const state = get();
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

    cycleStarting = true;
    cycleAbort?.abort();
    void cancelAllPings();
    const ac = new AbortController();
    cycleAbort = ac;

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

      const live = new Map<string, LocalPingResult>();
      for (const row of get().ranked) {
        if (row.measured && row.avg_ping > 0) {
          live.set(row.config_hash, {
            configHash: row.config_hash,
            pingMs: row.avg_ping,
            measuredAt: Date.now(),
          });
        }
      }
      const inFlight = new Set<string>();
      set({
        progressTotal: configs.length,
        status: 'پینگ…',
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
          testingHashes: [...inFlight].slice(0, 12),
          progressDone: latestDone,
          progressTotal: latestTotal,
          status: latestHint || `پینگ… ${latestDone}/${latestTotal}`,
        });
      };

      const results = await runPingQueue(configs, {
        concurrency: 12,
        timeoutMs: 1400,
        tcpTimeoutMs: 220,
        tcpPrecheck: true,
        signal: ac.signal,
        onStart: (cfg) => {
          if (ac.signal.aborted) return;
          inFlight.add(cfg.configHash);
        },
        onProgress: (done, total, latest) => {
          if (ac.signal.aborted || !get().testing) return;
          inFlight.delete(latest.configHash);
          live.set(latest.configHash, latest);
          latestDone = done;
          latestTotal = total;
          latestHint =
            latest.pingMs > 0
              ? `${done}/${total} · ${Math.round(latest.pingMs)}ms`
              : `${done}/${total} · قطع`;

          const now = Date.now();
          if (now - lastLight >= 160) {
            lastLight = now;
            set({
              progressDone: done,
              progressTotal: total,
              status: latestHint,
            });
          }
          if (now - lastRank >= 600) {
            if (rankTimer) clearTimeout(rankTimer);
            flushRank();
          } else if (!rankTimer) {
            rankTimer = setTimeout(flushRank, 600 - (now - lastRank));
          }
        },
      });

      if (rankTimer) clearTimeout(rankTimer);

      if (ac.signal.aborted || !get().testing) {
        const partial = results.filter((r) => r.pingMs !== undefined);
        partial.forEach((r) => {
          if (r.pingMs !== -1 || live.has(r.configHash)) live.set(r.configHash, r);
        });
        setTimeout(() => {
          try {
            const ranked = rankLocally(configs, live);
            set({ ranked, testingHashes: [], testing: false });
            void saveSnapshot(configs, ranked, {
              lastBackgroundStatus: 'تست متوقف شد — آخرین ترتیب ذخیره شد',
            });
          } catch {
            /* ignore */
          }
        }, 50);
        return;
      }

      results.forEach((r) => live.set(r.configHash, r));
      const ranked = rankLocally(configs, live);
      const ok = results.filter((r) => r.pingMs > 0).length;
      const baseStatus = `تمام · ${ok}/${configs.length} موفق`;
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
        lastBackgroundStatus: `آخرین تست · ${ok}/${configs.length} موفق`,
        operator: DEFAULT_OPERATOR,
        cycleIntervalMs: intervalOf(get),
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
        status: 'اول تست بگیر — فقط پینگ‌های موفق روی پنل اعمال می‌شوند',
      });
      return;
    }

    set({
      syncingPanel: true,
      error: null,
      status: `اعمال فوری · ${items.length} دامنه…`,
    });
    await new Promise<void>((r) => setTimeout(r, 0));

    const { configs, ranked } = get();
    void uploadCyclePings(
      DEFAULT_OPERATOR,
      configs,
      ranked
        .filter((r) => r.measured && r.avg_ping > 0)
        .map((r) => ({
          configHash: r.config_hash,
          pingMs: r.avg_ping,
          measuredAt: Date.now(),
        })),
    );

    let res = await pushPanelOrderNow(items);
    if (res.ok && res.count === 0 && res.source !== 'client') {
      set({ status: 'همگام‌سازی از میانگین ۱۵دقیقه…' });
      await uploadCyclePings(
        DEFAULT_OPERATOR,
        configs,
        ranked
          .filter((r) => r.measured && r.avg_ping > 0)
          .map((r) => ({
            configHash: r.config_hash,
            pingMs: r.avg_ping,
            measuredAt: Date.now(),
          })),
      );
      await new Promise<void>((r) => setTimeout(r, 400));
      res = await pushPanelOrderNow();
    }

    if (!res.ok) {
      set({
        syncingPanel: false,
        error: res.error || 'همگام‌سازی پنل ناموفق',
        status: 'خطا در اعمال ترتیب',
      });
      return;
    }
    if (res.count === 0) {
      set({
        syncingPanel: false,
        status: 'دامنهٔ منطبق در پنل نبود — هاست/پورت/SNI را چک کن',
      });
      return;
    }
    const msHint = res.ms != null ? ` · ${Math.round(res.ms)}ms` : '';
    set({
      syncingPanel: false,
      status: `ترتیب پنل اعمال شد · ${res.count} دامنه${msHint}`,
    });
  },
}));

export { DEFAULT_CYCLE_MS as CYCLE_MS };
