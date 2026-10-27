import { cancelAllPings, measureSpeed } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface SpeedQueueOptions {
  /** Parallel SOCKS cores — keep low (bandwidth contention). */
  concurrency?: number;
  downloadBudgetMs?: number;
  uploadBudgetMs?: number;
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

/**
 * Run short download+upload bursts only for configs that already have ping > 0.
 * Merges speed fields into a copy of the ping results map.
 */
export async function runSpeedQueue(
  configs: ParsedConfig[],
  pingResults: LocalPingResult[],
  options: SpeedQueueOptions = {},
): Promise<LocalPingResult[]> {
  const byHash = new Map(pingResults.map((r) => [r.configHash, { ...r }]));
  const alive = configs.filter((c) => {
    const r = byHash.get(c.configHash);
    return !!r && r.pingMs > 0;
  });

  if (alive.length === 0) return pingResults.map((r) => ({ ...r }));

  const concurrency = Math.max(1, Math.min(options.concurrency ?? 2, 3));
  const dlBudget = options.downloadBudgetMs ?? 1600;
  const ulBudget = options.uploadBudgetMs ?? 1400;
  const total = alive.length;
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

  async function worker(): Promise<void> {
    while (true) {
      if (stopped || options.signal?.aborted) return;
      const i = nextIndex++;
      if (i >= total) return;
      const cfg = alive[i]!;
      const prev = byHash.get(cfg.configHash)!;
      let downloadKbps = -1;
      let uploadKbps = -1;
      try {
        if (stopped || options.signal?.aborted) throw abortedError();
        const speed = await measureSpeed(cfg.raw, dlBudget, ulBudget);
        downloadKbps = speed.downloadKbps;
        uploadKbps = speed.uploadKbps;
      } catch (e) {
        if (isAbort(e) || stopped) return;
      }
      if (stopped || options.signal?.aborted) return;
      const latest: LocalPingResult = {
        ...prev,
        downloadKbps,
        uploadKbps,
        measuredAt: Date.now(),
      };
      byHash.set(cfg.configHash, latest);
      completed += 1;
      options.onProgress?.(completed, total, latest);
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(concurrency, total) }, () => worker()),
  );

  return configs.map((c) => {
    const r = byHash.get(c.configHash);
    if (r) return r;
    return {
      configHash: c.configHash,
      pingMs: -1,
      measuredAt: Date.now(),
      downloadKbps: -1,
      uploadKbps: -1,
    };
  });
}
