import { cancelAllPings, measurePacketLoss } from '../native/XrayPing';
import type { LocalPingResult, ParsedConfig } from '../types';

export interface PacketQueueOptions {
  concurrency?: number;
  probes?: number;
  perProbeTimeoutMs?: number;
  /** alive = only ping>0; all = every config */
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

/**
 * Packet-loss pass for targeted configs. Writes packetLossPct (−1 if failed).
 */
export async function runPacketQueue(
  configs: ParsedConfig[],
  pingResults: LocalPingResult[],
  options: PacketQueueOptions = {},
): Promise<LocalPingResult[]> {
  const byHash = new Map(pingResults.map((r) => [r.configHash, { ...r }]));
  const scope = options.scope ?? 'alive';

  const targets = configs.filter((c) => {
    if (scope === 'all') return true;
    const r = byHash.get(c.configHash);
    return !!r && r.pingMs > 0;
  });

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
  const probes = options.probes ?? 10;
  const perMs = options.perProbeTimeoutMs ?? 1500;
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
      let packetLossPct = -1;
      try {
        if (stopped || options.signal?.aborted) throw abortedError();
        const r = await measurePacketLoss(cfg.raw, probes, perMs);
        packetLossPct = r.lossPct;
      } catch (e) {
        if (isAbort(e) || stopped) return;
        packetLossPct = -1;
      }
      if (stopped || options.signal?.aborted) return;
      const latest: LocalPingResult = {
        ...prev,
        pingMs: prev.pingMs > 0 ? prev.pingMs : scope === 'all' ? 0 : prev.pingMs,
        packetLossPct,
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
      packetLossPct: -1,
    };
  });
}
