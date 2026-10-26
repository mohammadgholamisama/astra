import { pingRealDelay, pingTcp } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface PingQueueOptions {
  concurrency?: number;
  timeoutMs?: number;
  tcpPrecheck?: boolean;
  tcpTimeoutMs?: number;
  onStart?: (config: ParsedConfig) => void;
  onProgress?: (done: number, total: number, latest: LocalPingResult) => void;
  signal?: AbortSignal;
}

function abortedError() {
  return new DOMException('Aborted', 'AbortError');
}

/** Reject as soon as AbortSignal fires — native pings can't be killed, but workers stop waiting. */
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

export async function runPingQueue(
  configs: ParsedConfig[],
  options: PingQueueOptions = {},
): Promise<LocalPingResult[]> {
  const concurrency = Math.max(1, options.concurrency ?? 28);
  const timeoutMs = options.timeoutMs ?? 1600;
  const tcpTimeoutMs = options.tcpTimeoutMs ?? 280;
  const tcpPrecheck = options.tcpPrecheck !== false;
  const total = configs.length;
  const results: LocalPingResult[] = new Array(total);
  let nextIndex = 0;
  let completed = 0;

  async function measureOne(cfg: ParsedConfig): Promise<number> {
    options.onStart?.(cfg);
    if (options.signal?.aborted) throw abortedError();
    if (tcpPrecheck) {
      const tcp = await raceAbort(
        pingTcp(cfg.address, cfg.port, tcpTimeoutMs),
        options.signal,
      );
      if (tcp < 0) return -1;
    }
    if (options.signal?.aborted) throw abortedError();
    return raceAbort(pingRealDelay(cfg.raw, timeoutMs), options.signal);
  }

  async function worker(): Promise<void> {
    while (true) {
      if (options.signal?.aborted) return;
      const i = nextIndex++;
      if (i >= total) return;
      const cfg = configs[i]!;
      let pingMs = -1;
      try {
        pingMs = await measureOne(cfg);
      } catch (e) {
        if (
          (e instanceof DOMException && e.name === 'AbortError') ||
          (e instanceof Error && /abort/i.test(e.message))
        ) {
          return;
        }
        pingMs = -1;
      }
      if (options.signal?.aborted) return;
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

  if (options.signal?.aborted) {
    throw abortedError();
  }
  return results.map((r, i) => {
    if (r) return r;
    return {
      configHash: configs[i]!.configHash,
      pingMs: -1,
      measuredAt: Date.now(),
    };
  });
}
