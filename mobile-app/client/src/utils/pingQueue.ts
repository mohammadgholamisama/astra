import { cancelAllPings, pingRealDelay } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';
import { computeLatencyStats } from './latencyStats';

export interface PingQueueOptions {
  concurrency?: number;
  /** Wall budget passed to native (LibXray itself uses ~12s + 2 HTTP tries). */
  timeoutMs?: number;
  /** Extra full measure after a miss (native already retries HTTP twice). */
  retries?: number;
  /**
   * Independent Real-Delay samples for avg / jitter / stability.
   * 1 = classic single ping (+ retries on miss).
   */
  samples?: number;
  /**
   * Optional per-sample destination URLs (stability: 6 services).
   * Index i is used for sample i; missing → default multi-URL fallback.
   */
  probeUrls?: readonly string[];
  /** Include jitterMs / stabilityPct on results (default true when samples>1). */
  wantJitter?: boolean;
  wantStability?: boolean;
  onStart?: (config: ParsedConfig) => void;
  onProgress?: (done: number, total: number, latest: LocalPingResult) => void;
  signal?: AbortSignal;
}

function abortedError() {
  return new DOMException('Aborted', 'AbortError');
}

function isAbort(e: unknown): boolean {
  return (
    (e instanceof DOMException && e.name === 'AbortError') ||
    (e instanceof Error && /abort/i.test(e.message))
  );
}

/** Cap hung native calls without rejecting the worker. */
function raceMs(promise: Promise<number>, ms: number): Promise<number> {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      resolve(-1);
    }, Math.max(400, ms));
    promise.then(
      (v) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(typeof v === 'number' && !Number.isNaN(v) ? v : -1);
      },
      () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(-1);
      },
    );
  });
}

function raceAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortedError());
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortedError());
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(
      (v) => {
        signal.removeEventListener('abort', onAbort);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener('abort', onAbort);
        reject(e);
      },
    );
  });
}

/**
 * v2rayNG / v2box style queue:
 * Libv2ray.measureOutboundDelay on each share link (no TCP pre-fail gate).
 * Native does 12s × 2 HTTP attempts — JS must not cut that short.
 */
export async function runPingQueue(
  configs: ParsedConfig[],
  options: PingQueueOptions = {},
): Promise<LocalPingResult[]> {
  // v2rayNG runs several workers; too many cores thrash phones
  const concurrency = Math.max(1, Math.min(options.concurrency ?? 5, 8));
  // Match LibXray measureInstDelay window; JS race must not cut it short
  const timeoutMs = options.timeoutMs ?? 12_000;
  const retries = Math.max(0, options.retries ?? 1);
  const probeUrls = options.probeUrls ?? [];
  const samples = Math.max(
    1,
    Math.min(options.samples ?? (probeUrls.length > 0 ? probeUrls.length : 1), 8),
  );
  const wantJitter = options.wantJitter ?? samples > 1;
  const wantStability = options.wantStability ?? samples > 1;
  const total = configs.length;
  const results: LocalPingResult[] = new Array(total);
  let nextIndex = 0;
  let completed = 0;
  let stopped = false;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    cancelAllPings();
  };

  if (options.signal) {
    if (options.signal.aborted) stop();
    else options.signal.addEventListener('abort', stop, { once: true });
  }

  async function realDelayOnce(
    raw: string,
    destUrl?: string,
  ): Promise<number> {
    return raceAbort(
      raceMs(pingRealDelay(raw, timeoutMs, destUrl), timeoutMs + 3_000),
      options.signal,
    );
  }

  async function measureOne(cfg: ParsedConfig): Promise<LocalPingResult> {
    if (stopped || options.signal?.aborted) throw abortedError();
    options.onStart?.(cfg);
    if (stopped || options.signal?.aborted) throw abortedError();

    const taken: number[] = [];
    for (let s = 0; s < samples; s++) {
      if (stopped || options.signal?.aborted) throw abortedError();
      const dest = probeUrls[s]?.trim() || undefined;
      let ms = await realDelayOnce(cfg.raw, dest);
      // Single-sample mode: one retry on miss (legacy behavior)
      if (samples === 1) {
        for (let attempt = 0; attempt < retries && ms <= 0; attempt++) {
          if (stopped || options.signal?.aborted) throw abortedError();
          await new Promise<void>((r) => setTimeout(r, 150));
          if (stopped || options.signal?.aborted) throw abortedError();
          ms = await realDelayOnce(cfg.raw, dest);
        }
      }
      taken.push(ms);
    }

    const stats = computeLatencyStats(taken);
    const latest: LocalPingResult = {
      configHash: cfg.configHash,
      pingMs: stats.pingMs,
      measuredAt: Date.now(),
    };
    if (wantJitter) latest.jitterMs = stats.jitterMs;
    if (wantStability) latest.stabilityPct = stats.stabilityPct;
    return latest;
  }

  async function worker(): Promise<void> {
    while (true) {
      if (stopped || options.signal?.aborted) return;
      const i = nextIndex++;
      if (i >= total) return;
      const cfg = configs[i]!;
      let latest: LocalPingResult = {
        configHash: cfg.configHash,
        pingMs: -1,
        measuredAt: Date.now(),
      };
      try {
        latest = await measureOne(cfg);
      } catch (e) {
        if (isAbort(e) || stopped) {
          if (!results[i]) {
            results[i] = latest;
            completed += 1;
            try {
              options.onProgress?.(completed, total, results[i]!);
            } catch {
              /* ignore */
            }
          }
          return;
        }
      }
      if (stopped || options.signal?.aborted) {
        if (!results[i]) {
          results[i] = latest;
          completed += 1;
          try {
            options.onProgress?.(completed, total, results[i]!);
          } catch {
            /* ignore */
          }
        }
        return;
      }
      results[i] = latest;
      completed += 1;
      options.onProgress?.(completed, total, latest);
    }
  }

  await Promise.all(
    Array.from(
      { length: Math.min(concurrency, Math.max(total, 1)) },
      () => worker(),
    ),
  );

  return results.map((r, i) => {
    if (r) return r;
    return {
      configHash: configs[i]!.configHash,
      pingMs: -1,
      measuredAt: Date.now(),
    };
  });
}
