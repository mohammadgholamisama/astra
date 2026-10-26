import { create } from 'zustand';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { loadConfigsFromSubUrl } from '../utils/subParser';
import { runPingQueue } from '../utils/pingQueue';
import { configsAsPending, rankLocally } from '../utils/localRank';
import {
  ensureBackgroundPermissions,
  startBackgroundMonitor,
  stopBackgroundMonitor,
} from '../native/BackgroundMonitor';
import { uploadCyclePings } from '../api/uploadPings';
import type { LocalPingResult, Operator, ParsedConfig, RankedConfig } from '../types';

const CYCLE_MS = 5 * 60 * 1000;
const STORAGE_KEY = 'v2ray_ping_settings_v1';

interface AppState {
  operator: Operator | null;
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

  hydrate: () => Promise<void>;
  setOperator: (op: Operator) => void;
  setSubUrl: (url: string) => void;
  setBackgroundEnabled: (on: boolean) => Promise<void>;
  tick: () => void;
  loadSub: () => Promise<void>;
  runCycle: (manual?: boolean) => Promise<void>;
}

async function persist(partial: Record<string, unknown>) {
  try {
    const raw = await AsyncStorage.getItem(STORAGE_KEY);
    const prev = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify({ ...prev, ...partial }));
  } catch {
    /* ignore */
  }
}

export const useAppStore = create<AppState>((set, get) => ({
  operator: null,
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
  backgroundEnabled: true,

  hydrate: async () => {
    try {
      const raw = await AsyncStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw) as {
          operator?: Operator;
          subUrl?: string;
          ranked?: RankedConfig[];
          configs?: ParsedConfig[];
          nextRunAt?: number;
          backgroundEnabled?: boolean;
          lastBackgroundStatus?: string;
        };
        const nextRunAt = saved.nextRunAt ?? null;
        set({
          operator: saved.operator ?? null,
          subUrl: saved.subUrl ?? '',
          ranked: saved.ranked ?? [],
          configs: saved.configs ?? [],
          nextRunAt,
          remainingMs: nextRunAt ? Math.max(0, nextRunAt - Date.now()) : CYCLE_MS,
          backgroundEnabled: saved.backgroundEnabled !== false,
          status: saved.lastBackgroundStatus ?? 'آماده',
          hydrated: true,
        });
        // Resume FGS quietly if already enabled — don't re-prompt permissions on cold start.
        if (saved.backgroundEnabled !== false && saved.operator && (saved.subUrl || (saved.configs?.length ?? 0) > 0)) {
          setTimeout(() => {
            void startBackgroundMonitor(CYCLE_MS, { skipPermissionGate: true });
          }, 1500);
        }
        return;
      }
    } catch {
      /* ignore */
    }
    set({ hydrated: true });
  },

  setOperator: (op) => {
    set({ operator: op });
    void persist({ operator: op });
  },

  setSubUrl: (url) => {
    set({ subUrl: url });
    void persist({ subUrl: url });
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
        await persist({ backgroundEnabled: false });
        return;
      }

      set({ backgroundEnabled: true, error: null });
      await persist({ backgroundEnabled: true });

      const { operator, subUrl, configs } = get();
      if (operator && (subUrl.trim() || configs.length > 0)) {
        const started = await startBackgroundMonitor(CYCLE_MS, { skipPermissionGate: true });
        const nextRunAt = Date.now() + CYCLE_MS;
        set({
          status: started
            ? perms.batteryUnrestricted
              ? 'پایش پس‌زمینه فعال شد'
              : 'پایش فعال شد · محدودیت باتری را هم بردارید'
            : 'سرویس پس‌زمینه شروع نشد',
          nextRunAt,
          remainingMs: CYCLE_MS,
        });
        await persist({ nextRunAt });
      } else {
        set({ status: 'دسترسی OK — اول اپراتور و ساب را بگذار' });
      }
    } else {
      await stopBackgroundMonitor();
      set({
        backgroundEnabled: false,
        status: 'پایش پس‌زمینه خاموش شد',
        nextRunAt: null,
        remainingMs: CYCLE_MS,
      });
      await persist({ backgroundEnabled: false });
    }
  },

  tick: () => {
    const { nextRunAt, testing } = get();
    if (!nextRunAt || testing) return;
    const remaining = Math.max(0, nextRunAt - Date.now());
    set({ remainingMs: remaining });
    // Background service owns the schedule when enabled; UI timer is display-only
    // Manual/foreground auto only if background is off
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
      const configs = await loadConfigsFromSubUrl(subUrl.trim());
      if (configs.length === 0) {
        set({
          error: 'هیچ کانفیگ معتبری در ساب پیدا نشد',
          status: 'خطا',
          configs: [],
          ranked: [],
        });
        return;
      }
      set({
        configs,
        ranked: configsAsPending(configs),
        status: `${configs.length} کانفیگ آماده تست`,
        error: null,
      });
      await persist({ configs });

      const { backgroundEnabled, operator } = get();
      if (backgroundEnabled && operator) {
        await startBackgroundMonitor(CYCLE_MS);
        const nextRunAt = Date.now() + CYCLE_MS;
        set({ nextRunAt, remainingMs: CYCLE_MS });
        await persist({ nextRunAt });
      }
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : 'خطا در دریافت ساب',
        status: 'خطا',
      });
    }
  },

  runCycle: async (manual = false) => {
    const state = get();
    if (state.testing) return;
    if (!state.operator) {
      set({ error: 'ابتدا اپراتور را انتخاب کنید' });
      return;
    }
    if (!state.subUrl.trim()) {
      set({ error: 'لینک ساب را وارد کنید' });
      return;
    }

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
      if (configs.length === 0 || manual) {
        set({ status: 'دریافت و پارس ساب…' });
        configs = await loadConfigsFromSubUrl(state.subUrl.trim());
        set({ configs, ranked: configsAsPending(configs) });
      }
      if (configs.length === 0) {
        throw new Error('ساب خالی است');
      }

      const live = new Map<string, LocalPingResult>();
      const inFlight = new Set<string>();
      set({
        progressTotal: configs.length,
        status: 'Real Delay · نمایش لحظه‌ای',
        ranked: rankLocally(state.operator, configs, live, inFlight),
      });

      const results = await runPingQueue(configs, {
        concurrency: 4,
        timeoutMs: 6500,
        tcpPrecheck: true,
        onStart: (cfg) => {
          inFlight.add(cfg.configHash);
          set({
            testingHashes: [...inFlight],
            ranked: rankLocally(state.operator!, configs, live, inFlight),
          });
        },
        onProgress: (done, total, latest) => {
          inFlight.delete(latest.configHash);
          live.set(latest.configHash, latest);
          const ranked = rankLocally(state.operator!, configs, live, inFlight);
          const hint = latest.pingMs > 0 ? `${Math.round(latest.pingMs)}ms` : 'قطع';
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
      const ranked = rankLocally(state.operator, configs, live);
      const ok = results.filter((r) => r.pingMs > 0).length;
      const now = Date.now();
      const nextRunAt = now + CYCLE_MS;
      set({
        testing: false,
        testingHashes: [],
        ranked,
        nextRunAt,
        remainingMs: CYCLE_MS,
        status: `تمام · ${ok}/${configs.length} موفق`,
        progressDone: configs.length,
        progressTotal: configs.length,
      });
      await persist({
        configs,
        ranked,
        nextRunAt,
        lastBackgroundStatus: `آخرین تست · ${ok}/${configs.length} موفق`,
      });

      // Live upload — hub keeps last 10m per device, then reorders Astra
      void uploadCyclePings(state.operator!, configs, results).then((sent) => {
        if (sent) {
          set({ status: `تمام · ${ok}/${configs.length} موفق · ارسال به سرور` });
        }
      });

      if (get().backgroundEnabled) {
        await startBackgroundMonitor(CYCLE_MS);
      }
    } catch (e) {
      const now = Date.now();
      const aborted =
        (e instanceof DOMException && e.name === 'AbortError') ||
        (e instanceof Error && /abort/i.test(e.message));
      set({
        testing: false,
        testingHashes: [],
        error: aborted ? null : e instanceof Error ? e.message : 'خطای ناشناخته',
        status: aborted ? 'متوقف شد' : 'خطا در چرخه',
        nextRunAt: now + CYCLE_MS,
        remainingMs: CYCLE_MS,
      });
    }
  },
}));

export { CYCLE_MS };
