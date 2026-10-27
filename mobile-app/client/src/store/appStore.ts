import { create } from 'zustand';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { runSpeedQueue } from '../utils/speedQueue';
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
  /** After ping: also measure download/upload and rank by speed. */
  speedRankEnabled: boolean;
  syncingPanel: boolean;
  /** Short result line under «اعمال ترتیب» */
  panelSyncHint: string;
  permissionsReady: boolean;

  hydrate: () => Promise<void>;
  setSubUrl: (url: string) => void;
  setCycleInterval: (ms: number) => Promise<void>;
  setBackgroundEnabled: (on: boolean) => Promise<void>;
  setSpeedRankEnabled: (on: boolean) => Promise<void>;
  tick: () => void;
  loadSub: () => Promise<void>;
  runCycle: (manual?: boolean) => Promise<void>;
  cancelCycle: () => void;
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

/** Upload this device's pings, then reorder panel from multi-device 15m window. */
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
    panelSyncHint: 'ارسال پینگ و اعمال ترتیب…',
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

    set({
      status: sent
        ? 'میانگین ۱۵د · اعمال ترتیب پنل…'
        : 'اعمال ترتیب پنل از پنجرهٔ سرور…',
      panelSyncHint: 'اعمال روی پنل…',
    });

    // Brief yield so Redis ingest is visible to RankedFingerprints
    await new Promise<void>((r) => setTimeout(r, 400));
    const res = await pushPanelOrderNow();

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
      clearPanelSyncBusy(set, {
        panelSyncHint: 'دامنهٔ منطبق نبود / میانگین ناکافی',
        status: `${baseStatus} · هنوز میانگین ۱۵د کافی نیست`,
      });
      return;
    }
    const msHint = res.ms != null ? ` · ${Math.round(res.ms)}ms` : '';
    clearPanelSyncBusy(set, {
      panelSyncHint: `اعمال شد · ${res.count} دامنه`,
      status: `${baseStatus} · پنل ${res.count} دامنه (میانگین ۱۵د)${msHint}`,
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
  speedRankEnabled: false,
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
        speedRankEnabled: saved.speedRankEnabled === true,
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
      set({
        status: get().backgroundEnabled
          ? `چرخه هر ${cycleMinutesLabel(cycleIntervalMs)} دقیقه تنظیم شد`
          : `بازه ${cycleMinutesLabel(cycleIntervalMs)}د — برای چرخش خودکار پایش را روشن کن`,
      });
    }
  },

  setSpeedRankEnabled: async (on) => {
    set({
      speedRankEnabled: on,
      status: on
        ? 'رتبه با سرعت: اول پینگ، بعد دانلود/آپلود روی وصل‌ها'
        : 'رتبه فقط با پینگ',
    });
    await saveSettings({ speedRankEnabled: on });
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

  cancelCycle: () => {
    // Idempotent — ignore spam taps / onPressIn+onPress doubles
    if (!get().testing && !cycleStarting && !cycleAbort) return;
    cancelCooldownUntil = Date.now() + 750;
    // Flip UI FIRST so the button feels instant even under load
    set({
      testing: false,
      testingHashes: [],
      syncingPanel: false,
      status: 'تست متوقف شد',
    });
    cycleAbort?.abort();
    cycleAbort = null;
    cycleStarting = false;
    cancelAllPings(); // sync fire-and-forget — never await
    const { configs, ranked } = get();
    if (configs.length > 0) {
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

    cycleStarting = true;
    cycleAbort?.abort();
    cancelAllPings();
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
        // Light updates only — keep cancel button responsive
        set({
          ranked: rankLocally(configs, live, inFlight),
          testingHashes: [...inFlight].slice(0, 6),
          progressDone: latestDone,
          progressTotal: latestTotal,
          status: latestHint || `پینگ… ${latestDone}/${latestTotal}`,
        });
      };

      let results = await runPingQueue(configs, {
        concurrency: 5,
        timeoutMs: 2200,
        tcpTimeoutMs: 600,
        tcpPrecheck: 'soft',
        retries: 1,
        signal: ac.signal,
        onStart: (cfg) => {
          if (ac.signal.aborted || !get().testing) return;
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
          const nearEnd = total > 0 && done >= Math.max(1, total - 4);
          if (nearEnd || now - lastLight >= 250) {
            lastLight = now;
            set({
              progressDone: done,
              progressTotal: total,
              status: latestHint,
            });
          }
          if (now - lastRank >= 1800) {
            if (rankTimer) clearTimeout(rankTimer);
            flushRank();
          } else if (!rankTimer) {
            rankTimer = setTimeout(flushRank, 1800 - (now - lastRank));
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

      // Optional: speed burst only on alive configs, then re-rank
      if (get().speedRankEnabled && !ac.signal.aborted && get().testing) {
        const aliveN = results.filter((r) => r.pingMs > 0).length;
        set({
          status: `سرعت · ${aliveN} وصل…`,
          progressDone: 0,
          progressTotal: Math.max(1, aliveN),
          testingHashes: [],
        });
        lastLight = 0;
        lastRank = 0;
        results = await runSpeedQueue(configs, results, {
          concurrency: 2,
          downloadBudgetMs: 1600,
          uploadBudgetMs: 1400,
          signal: ac.signal,
          onProgress: (done, total, latest) => {
            if (ac.signal.aborted || !get().testing) return;
            live.set(latest.configHash, latest);
            const dl =
              latest.downloadKbps && latest.downloadKbps > 0
                ? `${Math.round(latest.downloadKbps)}↓`
                : '—';
            const ul =
              latest.uploadKbps && latest.uploadKbps > 0
                ? `${Math.round(latest.uploadKbps)}↑`
                : '—';
            const hint = `${done}/${total} · ${dl} ${ul} kbps`;
            const now = Date.now();
            if (done >= total - 1 || now - lastLight >= 200) {
              lastLight = now;
              set({
                progressDone: done,
                progressTotal: total,
                status: hint,
              });
            }
            if (now - lastRank >= 1600) {
              lastRank = now;
              set({ ranked: rankLocally(configs, live) });
            }
          },
        });
        results.forEach((r) => live.set(r.configHash, r));
      }

      if (ac.signal.aborted || !get().testing) {
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

      const ranked = rankLocally(configs, live);
      const ok = results.filter((r) => r.pingMs > 0).length;
      const withSpeed = results.filter(
        (r) => (r.downloadKbps ?? 0) > 0 || (r.uploadKbps ?? 0) > 0,
      ).length;
      const baseStatus = get().speedRankEnabled
        ? `تمام · ${ok} پینگ · ${withSpeed} سرعت`
        : `تمام · ${ok}/${configs.length} موفق`;
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
        speedRankEnabled: get().speedRankEnabled,
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

      let res = await pushPanelOrderNow(items);
      if (res.ok && res.count === 0 && res.source !== 'client') {
        set({
          status: 'همگام‌سازی از میانگین ۱۵دقیقه…',
          panelSyncHint: 'میانگین ۱۵د…',
        });
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
        clearPanelSyncBusy(set, {
          error: res.error || 'همگام‌سازی پنل ناموفق',
          panelSyncHint: res.error || 'ناموفق — دوباره بزن',
          status: 'خطا در اعمال ترتیب',
        });
        return;
      }
      if (res.count === 0) {
        clearPanelSyncBusy(set, {
          panelSyncHint: 'دامنهٔ منطبق نبود',
          status: 'دامنهٔ منطبق در پنل نبود — هاست/پورت/SNI را چک کن',
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
