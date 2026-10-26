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
import {
  loadConfigs,
  loadRanked,
  loadSettings,
  saveSettings,
  saveSnapshot,
} from './persist';
import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';
import { DEFAULT_OPERATOR } from '../types';

const CYCLE_MS = 5 * 60 * 1000;

interface AppState {
  subUrl: string;
  configs: ParsedConfig[];
  ranked: RankedConfig[];
  nextRunAt: number | null;
  remainingMs: number;
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
  setBackgroundEnabled: (on: boolean) => Promise<void>;
  tick: () => void;
  loadSub: () => Promise<void>;
  runCycle: (manual?: boolean) => Promise<void>;
  cancelCycle: () => void;
  syncPanelOrder: () => Promise<void>;
}

let cycleAbort: AbortController | null = null;

async function armTimer(set: (p: Partial<AppState>) => void) {
  const nextRunAt = Date.now() + CYCLE_MS;
  set({ nextRunAt, remainingMs: CYCLE_MS });
  await saveSettings({ nextRunAt });
  return nextRunAt;
}

export const useAppStore = create<AppState>((set, get) => ({
  subUrl: '',
  configs: [],
  ranked: [],
  nextRunAt: null,
  remainingMs: CYCLE_MS,
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
    try {
      const [saved, configs, ranked] = await Promise.all([
        loadSettings(),
        loadConfigs(),
        loadRanked(),
      ]);

      const nextRunAt = saved.nextRunAt ?? null;
      const hasCache = configs.length > 0;
      set({
        subUrl: saved.subUrl ?? '',
        configs,
        ranked:
          ranked.length > 0
            ? ranked
            : hasCache
              ? configsAsPending(configs)
              : [],
        nextRunAt,
        remainingMs: nextRunAt ? Math.max(0, nextRunAt - Date.now()) : CYCLE_MS,
        status: hasCache
          ? saved.lastBackgroundStatus ?? `${configs.length} کانفیگ از حافظه`
          : saved.lastBackgroundStatus ?? 'آماده',
        hydrated: true,
      });

      // Keep a stable operator tag for hub uploads (no UI)
      await saveSettings({ operator: saved.operator ?? DEFAULT_OPERATOR });

      const perms = await ensureBackgroundPermissions();
      await saveSettings({ permissionsAsked: true });

      if (perms.ok) {
        const wantBg = saved.backgroundEnabled !== false;
        set({
          backgroundEnabled: wantBg,
          permissionsReady: true,
          error: null,
        });
        await saveSettings({ backgroundEnabled: wantBg });

        if (wantBg && (saved.subUrl || configs.length > 0)) {
          setTimeout(() => {
            void startBackgroundMonitor(CYCLE_MS, { skipPermissionGate: true });
          }, 800);
        }
      } else {
        set({
          backgroundEnabled: false,
          permissionsReady: true,
          error: perms.message ?? 'دسترسی‌های لازم داده نشد',
          status: hasCache
            ? `${configs.length} کانفیگ از حافظه`
            : 'برای پایش پس‌زمینه اجازه بدهید',
        });
        await saveSettings({ backgroundEnabled: false });
      }
    } catch {
      set({ hydrated: true, permissionsReady: true, backgroundEnabled: false });
    }
  },

  setSubUrl: (url) => {
    set({ subUrl: url });
    void saveSettings({ subUrl: url });
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

      set({ backgroundEnabled: true, error: null });
      await saveSettings({ backgroundEnabled: true, operator: DEFAULT_OPERATOR });

      const { subUrl, configs } = get();
      if (subUrl.trim() || configs.length > 0) {
        const started = await startBackgroundMonitor(CYCLE_MS, {
          skipPermissionGate: true,
        });
        await armTimer(set);
        set({
          status: started
            ? perms.batteryUnrestricted
              ? 'پایش پس‌زمینه فعال شد'
              : 'پایش فعال شد · محدودیت باتری را هم بردارید'
            : 'سرویس پس‌زمینه شروع نشد',
        });
      } else {
        set({ status: 'دسترسی OK — اول لینک ساب را بگذار' });
      }
    } else {
      await stopBackgroundMonitor();
      set({
        backgroundEnabled: false,
        status: 'پایش پس‌زمینه خاموش شد',
        nextRunAt: null,
        remainingMs: CYCLE_MS,
      });
      await saveSettings({ backgroundEnabled: false, nextRunAt: undefined });
    }
  },

  tick: () => {
    const { nextRunAt, testing } = get();
    if (!nextRunAt || testing) return;
    const remaining = Math.max(0, nextRunAt - Date.now());
    set({ remainingMs: remaining });
    if (remaining === 0 && !get().backgroundEnabled) {
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
      });

      if (get().backgroundEnabled) {
        await startBackgroundMonitor(CYCLE_MS, { skipPermissionGate: true });
        await armTimer(set);
      }
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : 'خطا در دریافت ساب',
        status: 'خطا',
      });
    }
  },

  cancelCycle: () => {
    if (!get().testing) return;
    cycleAbort?.abort();
    set({
      testing: false,
      testingHashes: [],
      status: 'تست متوقف شد',
    });
  },

  runCycle: async (manual = false) => {
    const state = get();
    if (state.testing) return;
    if (!state.subUrl.trim() && state.configs.length === 0) {
      set({ error: 'لینک ساب را وارد کنید' });
      return;
    }

    cycleAbort?.abort();
    const ac = new AbortController();
    cycleAbort = ac;

    set({
      testing: true,
      testingHashes: [],
      error: null,
      status: manual ? 'تست فوری…' : 'چرخه خودکار…',
      progressDone: 0,
      progressTotal: 0,
    });

    try {
      let configs = state.configs;
      if (configs.length === 0) {
        if (!state.subUrl.trim()) throw new Error('ساب خالی است');
        set({ status: 'دریافت و پارس ساب…' });
        configs = await loadConfigsFromSubUrl(state.subUrl.trim(), {
          signal: ac.signal,
          onStatus: (msg) => set({ status: msg }),
        });
        if (ac.signal.aborted) throw new DOMException('Aborted', 'AbortError');
        const pending = configsAsPending(configs);
        set({ configs, ranked: pending });
        await saveSnapshot(configs, pending);
      }
      if (configs.length === 0) {
        throw new Error('ساب خالی است');
      }

      const live = new Map<string, LocalPingResult>();
      for (const row of state.ranked) {
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
        status: 'پینگ واقعی · سریع',
        ranked: rankLocally(configs, live, inFlight),
      });

      const results = await runPingQueue(configs, {
        concurrency: 28,
        timeoutMs: 1600,
        tcpTimeoutMs: 280,
        tcpPrecheck: true,
        signal: ac.signal,
        onStart: (cfg) => {
          if (ac.signal.aborted) return;
          inFlight.add(cfg.configHash);
          set({
            testingHashes: [...inFlight],
            ranked: rankLocally(configs, live, inFlight),
          });
        },
        onProgress: (done, total, latest) => {
          inFlight.delete(latest.configHash);
          live.set(latest.configHash, latest);
          const ranked = rankLocally(configs, live, inFlight);
          const hint =
            latest.pingMs > 0 ? `${Math.round(latest.pingMs)}ms` : 'قطع';
          set({
            ranked,
            progressDone: done,
            progressTotal: total,
            testingHashes: [...inFlight],
            status: `${done}/${total} · آخرین: ${hint}`,
          });
        },
      });

      results.forEach((r) => live.set(r.configHash, r));
      const ranked = rankLocally(configs, live);
      const ok = results.filter((r) => r.pingMs > 0).length;
      set({
        testing: false,
        testingHashes: [],
        ranked,
        status: `تمام · ${ok}/${configs.length} موفق`,
        progressDone: configs.length,
        progressTotal: configs.length,
      });
      await armTimer(set);
      await saveSnapshot(configs, ranked, {
        lastBackgroundStatus: `آخرین تست · ${ok}/${configs.length} موفق`,
        operator: DEFAULT_OPERATOR,
      });

      void uploadCyclePings(DEFAULT_OPERATOR, configs, results).then((sent) => {
        if (sent) {
          set({
            status: `تمام · ${ok}/${configs.length} موفق · ارسال به سرور`,
          });
        }
      });

      if (get().backgroundEnabled) {
        await startBackgroundMonitor(CYCLE_MS, { skipPermissionGate: true });
      }
    } catch (e) {
      const aborted =
        (e instanceof DOMException && e.name === 'AbortError') ||
        (e instanceof Error && /abort/i.test(e.message));

      const { configs, ranked } = get();
      if (configs.length > 0 && ranked.length > 0) {
        await saveSnapshot(configs, ranked, {
          lastBackgroundStatus: aborted
            ? 'تست متوقف شد — آخرین ترتیب ذخیره شد'
            : undefined,
        });
      }

      set({
        testing: false,
        testingHashes: [],
        error: aborted ? null : e instanceof Error ? e.message : 'خطای ناشناخته',
        status: aborted ? 'تست متوقف شد' : 'خطا در چرخه',
      });
      await armTimer(set);
    } finally {
      if (cycleAbort === ac) cycleAbort = null;
    }
  },

  syncPanelOrder: async () => {
    if (get().syncingPanel || get().testing) return;

    // Exact averages from this screen → host:port:domain — no Redis wait
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

    // Background: keep hub window warm for scheduler (does not block apply)
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
    // Older hub ignores body items — fall back to window sync after a quick upload
    if (res.ok && res.count === 0 && res.source !== 'client') {
      set({ status: 'همگام‌سازی از پنجرهٔ سرور…' });
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

export { CYCLE_MS };
