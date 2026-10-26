import { pingRealDelay, pingTcp } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface PingQueueOptions {
  concurrency?: number;
  timeoutMs?: number;
  tcpPrecheck?: boolean;
  onStart?: (config: ParsedConfig) => void;
  onProgress?: (done: number, total: number, latest: LocalPingResult) => void;
  signal?: AbortSignal;
}

export async function runPingQueue(
  configs: ParsedConfig[],
  options: PingQueueOptions = {},
): Promise<LocalPingResult[]> {
  const concurrency = Math.max(1, options.concurrency ?? 4);
  const timeoutMs = options.timeoutMs ?? 6500;
  const tcpPrecheck = options.tcpPrecheck !== false;
  const total = configs.length;
  const results: LocalPingResult[] = new Array(total);
  let nextIndex = 0;
  let completed = 0;

  async function measureOne(cfg: ParsedConfig): Promise<number> {
    options.onStart?.(cfg);
    if (tcpPrecheck) {
      const tcp = await pingTcp(cfg.address, cfg.port, 1400);
      if (tcp < 0) return -1;
    }
    return pingRealDelay(cfg.raw, timeoutMs);
  }

  async function worker(): Promise<void> {
    while (true) {
      if (options.signal?.aborted) {
        throw new DOMException('Aborted', 'AbortError');
      }
      const i = nextIndex++;
      if (i >= total) return;
      const cfg = configs[i]!;
      const pingMs = await measureOne(cfg);
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

  const workers = Array.from({ length: Math.min(concurrency, Math.max(total, 1)) }, () =>
    worker(),
  );
  await Promise.all(workers);
  return results.filter(Boolean);
}
