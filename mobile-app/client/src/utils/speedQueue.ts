import { cancelAllPings, measureSpeed } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface SpeedQueueOptions {
  /** Parallel SOCKS cores — keep low (bandwidth contention). */
  concurrency?: number;
  downloadBudgetMs?: number;
  uploadBudgetMs?: number;
  /**
   * alive = only ping>0 (after ping pass)
   * all = every config from first to last (speed-only mode)
   */
  scope?: 'alive' | 'all';
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

async function measureWithRetry(
  raw: string,
  dlBudget: number,
  ulBudget: number,
  signal?: AbortSignal,
): Promise<{ downloadKbps: number; uploadKbps: number }> {
  let last = await measureSpeed(raw, dlBudget, ulBudget);
  if (signal?.aborted) throw abortedError();
  if (!(last.downloadKbps > 0)) {
    last = await measureSpeed(raw, dlBudget, ulBudget);
  }
  return last;
}

/**
 * Short download+upload bursts.
 * Always writes downloadKbps/uploadKbps on targeted rows (−1 if failed).
 */
export async function runSpeedQueue(
  configs: ParsedConfig[],
  pingResults: LocalPingResult[],
  options: SpeedQueueOptions = {},
): Promise<LocalPingResult[]> {
  const byHash = new Map(pingResults.map((r) => [r.configHash, { ...r }]));
  const scope = options.scope ?? 'alive';

  const targets = configs.filter((c) => {
    if (scope === 'all') return true;
    const r = byHash.get(c.configHash);
    return !!r && r.pingMs > 0;
  });

  // Ensure a result stub for every target (speed-only: pingMs=0 = skipped)
  for (const c of targets) {
    if (!byHash.has(c.configHash)) {
      byHash.set(c.configHash, {
        configHash: c.configHash,
        pingMs: 0,
        measuredAt: Date.now(),
      });
    }
  }

  if (targets.length === 0) return pingResults.map((r) => ({ ...r }));

  const concurrency = Math.max(1, Math.min(options.concurrency ?? 2, 3));
  const dlBudget = options.downloadBudgetMs ?? 1600;
  const ulBudget = options.uploadBudgetMs ?? 1400;
  const total = targets.length;
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
      const cfg = targets[i]!;
      const prev = byHash.get(cfg.configHash)!;
      let downloadKbps = -1;
      let uploadKbps = -1;
      try {
        if (stopped || options.signal?.aborted) throw abortedError();
        const speed = await measureWithRetry(
          cfg.raw,
          dlBudget,
          ulBudget,
          options.signal,
        );
        downloadKbps =
          speed.downloadKbps != null && Number.isFinite(speed.downloadKbps)
            ? speed.downloadKbps
            : -1;
        uploadKbps =
          speed.uploadKbps != null && Number.isFinite(speed.uploadKbps)
            ? speed.uploadKbps
            : -1;
      } catch (e) {
        if (isAbort(e) || stopped) return;
        downloadKbps = -1;
        uploadKbps = -1;
      }
      if (stopped || options.signal?.aborted) return;
      const latest: LocalPingResult = {
        ...prev,
        // Keep existing ping; speed-only stubs stay at 0
        pingMs: prev.pingMs > 0 ? prev.pingMs : scope === 'all' ? 0 : prev.pingMs,
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
