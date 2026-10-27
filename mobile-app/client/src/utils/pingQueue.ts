import { cancelAllPings, pingRealDelay, pingTcp } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface PingQueueOptions {
  concurrency?: number;
  /** How long to wait for LibXray real-delay (ms). */
  timeoutMs?: number;
  /**
   * soft (default): TCP fail still runs real-delay.
   * hard: TCP fail → قطع without real-delay.
   * off: skip TCP entirely.
   */
  tcpPrecheck?: 'soft' | 'hard' | 'off' | boolean;
  tcpTimeoutMs?: number;
  /** Extra real-delay attempts after a miss (default 1). */
  retries?: number;
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

function resolveTcpMode(
  v: PingQueueOptions['tcpPrecheck'],
): 'soft' | 'hard' | 'off' {
  if (v === false || v === 'off') return 'off';
  if (v === 'hard' || v === true) return v === true ? 'soft' : 'hard';
  if (v === 'soft') return 'soft';
  return 'soft';
}

/** Reject as soon as AbortSignal fires — does not wait for native. */
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
 * Hard wall-clock cap so a hung native LibXray call cannot freeze the queue.
 * Resolves -1 on timeout (not reject) so workers keep advancing.
 */
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

export async function runPingQueue(
  configs: ParsedConfig[],
  options: PingQueueOptions = {},
): Promise<LocalPingResult[]> {
  const concurrency = Math.max(1, options.concurrency ?? 3);
  const timeoutMs = options.timeoutMs ?? 4500;
  const tcpTimeoutMs = options.tcpTimeoutMs ?? 900;
  const tcpMode = resolveTcpMode(options.tcpPrecheck);
  const retries = Math.max(0, options.retries ?? 1);
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

  async function realDelayOnce(cfg: ParsedConfig): Promise<number> {
    // Wall clock a bit above native wait so we prefer native result when possible
    return raceAbort(
      raceMs(pingRealDelay(cfg.raw, timeoutMs), timeoutMs + 1200),
      options.signal,
    );
  }

  async function measureOne(cfg: ParsedConfig): Promise<number> {
    if (stopped || options.signal?.aborted) throw abortedError();
    options.onStart?.(cfg);
    if (stopped || options.signal?.aborted) throw abortedError();

    if (tcpMode !== 'off') {
      const tcp = await raceAbort(
        raceMs(pingTcp(cfg.address, cfg.port, tcpTimeoutMs), tcpTimeoutMs + 400),
        options.signal,
      );
      // Hard mode only: TCP unreachable → skip real-delay
      if (tcpMode === 'hard' && tcp < 0) return -1;
      // soft: continue to real-delay even if TCP timed out
    }

    if (stopped || options.signal?.aborted) throw abortedError();

    let best = await realDelayOnce(cfg);
    for (let attempt = 0; attempt < retries && best <= 0; attempt++) {
      if (stopped || options.signal?.aborted) throw abortedError();
      // Short pause so LibXray can tear down the failed outbound
      await new Promise<void>((r) => setTimeout(r, 120));
      if (stopped || options.signal?.aborted) throw abortedError();
      const again = await realDelayOnce(cfg);
      if (again > 0) best = again;
    }
    return best;
  }

  async function worker(): Promise<void> {
    while (true) {
      if (stopped || options.signal?.aborted) return;
      const i = nextIndex++;
      if (i >= total) return;
      if (stopped || options.signal?.aborted) return;
      const cfg = configs[i]!;
      let pingMs = -1;
      try {
        pingMs = await measureOne(cfg);
      } catch (e) {
        if (isAbort(e) || stopped) {
          if (!results[i]) {
            results[i] = {
              configHash: cfg.configHash,
              pingMs: -1,
              measuredAt: Date.now(),
            };
            completed += 1;
            try {
              options.onProgress?.(completed, total, results[i]!);
            } catch {
              /* ignore */
            }
          }
          return;
        }
        pingMs = -1;
      }
      if (stopped || options.signal?.aborted) {
        if (!results[i]) {
          results[i] = {
            configHash: cfg.configHash,
            pingMs: -1,
            measuredAt: Date.now(),
          };
          completed += 1;
          try {
            options.onProgress?.(completed, total, results[i]!);
          } catch {
            /* ignore */
          }
        }
        return;
      }
      const latest: LocalPingResult = {
        configHash: cfg.configHash,
        pingMs,
        measuredAt: Date.now(),
      };
      results[i] = latest;
      completed += 1;
      options.onProgress?.(completed, total, latest);
    }
  }

  const workers = Array.from(
    { length: Math.min(concurrency, Math.max(total, 1)) },
    () => worker(),
  );
  await Promise.all(workers);

  return results.map((r, i) => {
    if (r) return r;
    return {
      configHash: configs[i]!.configHash,
      pingMs: -1,
      measuredAt: Date.now(),
    };
  });
}
