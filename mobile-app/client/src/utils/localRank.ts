import type { LocalPingResult, Operator, ParsedConfig, RankedConfig } from '../types';
import { OPERATORS } from '../types';

type LiveMap = Map<string, LocalPingResult>;

/**
 * Live ranking: updates as each config finishes.
 * Untested stay at the bottom; successful rise by lowest ping.
 */
export function rankLocally(
  operator: Operator,
  configs: ParsedConfig[],
  results: LocalPingResult[] | LiveMap,
  testingHashes?: Set<string> | string | null,
): RankedConfig[] {
  const byHash: LiveMap =
    results instanceof Map ? results : new Map(results.map((r) => [r.configHash, r]));
  const opMeta = OPERATORS.find((o) => o.id === operator);
  const testingSet =
    testingHashes instanceof Set
      ? testingHashes
      : testingHashes
        ? new Set([testingHashes])
        : new Set<string>();

  const rows: RankedConfig[] = configs.map((c) => {
    const r = byHash.get(c.configHash);
    const tested = !!r;
    const ping = tested ? r!.pingMs : -1;
    const ok = tested && ping > 0;
    const testing = !tested && testingSet.has(c.configHash);

    return {
      rank: 0,
      config_hash: c.configHash,
      remark: c.remark,
      protocol: c.protocol,
      transport: c.transport,
      security: c.security,
      host: c.address,
      port: c.port,
      match_host: c.matchHost,
      success_rate: ok ? 1 : 0,
      avg_ping: tested ? (ok ? ping : -1) : testing ? -2 : -1,
      operator_pings: tested
        ? [
            {
              operator,
              label_fa: opMeta?.label ?? operator,
              ping_ms: ping,
              updated_at: r!.measuredAt,
              has_sample: true,
            },
          ]
        : [],
      updated_at: new Date(r?.measuredAt ?? Date.now()).toISOString(),
    };
  });

  rows.sort((a, b) => {
    const aTested = a.operator_pings.length > 0 || a.avg_ping === -2;
    const bTested = b.operator_pings.length > 0 || b.avg_ping === -2;
    const aDone = a.operator_pings.length > 0;
    const bDone = b.operator_pings.length > 0;

    // Done successes first, then done failures, then testing, then waiting
    const tier = (x: RankedConfig, done: boolean, testedFlag: boolean) => {
      if (done && x.success_rate > 0) return 0;
      if (done) return 1;
      if (x.avg_ping === -2) return 2;
      if (!testedFlag) return 3;
      return 3;
    };
    const ta = tier(a, aDone, aTested);
    const tb = tier(b, bDone, bTested);
    if (ta !== tb) return ta - tb;

    if (a.success_rate > 0 && b.success_rate > 0) {
      return a.avg_ping - b.avg_ping;
    }
    return a.remark.localeCompare(b.remark, 'fa');
  });

  return rows.map((row, i) => ({ ...row, rank: i + 1 }));
}

export function configsAsPending(configs: ParsedConfig[]): RankedConfig[] {
  return configs.map((c, i) => ({
    rank: i + 1,
    config_hash: c.configHash,
    remark: c.remark,
    protocol: c.protocol,
    transport: c.transport,
    security: c.security,
    host: c.address,
    port: c.port,
    match_host: c.matchHost,
    success_rate: 0,
    avg_ping: -1,
    operator_pings: [],
    updated_at: new Date().toISOString(),
  }));
}
