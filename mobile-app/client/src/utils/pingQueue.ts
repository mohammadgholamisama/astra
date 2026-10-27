import { cancelAllPings, pingRealDelay, pingTcp } from '../native/XrayPing';
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

function isAbort(e: unknown): boolean {
  return (
    (e instanceof DOMException && e.name === 'AbortError') ||
    (e instanceof Error && /abort/i.test(e.message))
  );
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

export async function runPingQueue(
  configs: ParsedConfig[],
  options: PingQueueOptions = {},
): Promise<LocalPingResult[]> {
  const concurrency = Math.max(1, options.concurrency ?? 5);
  const timeoutMs = options.timeoutMs ?? 1200;
  const tcpTimeoutMs = options.tcpTimeoutMs ?? 200;
  const tcpPrecheck = options.tcpPrecheck !== false;
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

  async function measureOne(cfg: ParsedConfig): Promise<number> {
    if (stopped || options.signal?.aborted) throw abortedError();
    options.onStart?.(cfg);
    if (stopped || options.signal?.aborted) throw abortedError();
    if (tcpPrecheck) {
      const tcp = await raceAbort(
        pingTcp(cfg.address, cfg.port, tcpTimeoutMs),
        options.signal,
      );
      if (tcp < 0) return -1;
    }
    if (stopped || options.signal?.aborted) throw abortedError();
    return raceAbort(pingRealDelay(cfg.raw, timeoutMs), options.signal);
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
        if (isAbort(e) || stopped) return;
        pingMs = -1;
      }
      if (stopped || options.signal?.aborted) return;
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
