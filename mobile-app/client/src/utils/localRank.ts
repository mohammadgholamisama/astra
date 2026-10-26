import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';

type LiveMap = Map<string, LocalPingResult>;

/**
 * Live ranking: updates as each config finishes.
 * Untested stay at the bottom; successful rise by lowest ping.
 */
export function rankLocally(
  configs: ParsedConfig[],
  results: LocalPingResult[] | LiveMap,
  testingHashes?: Set<string> | string | null,
): RankedConfig[] {
  const byHash: LiveMap =
    results instanceof Map
      ? results
      : new Map(results.map((r) => [r.configHash, r]));
  const testingSet =
    testingHashes instanceof Set
      ? testingHashes
      : testingHashes
        ? new Set([testingHashes])
        : new Set<string>();

  const rows: RankedConfig[] = configs.map((c) => {
    const r = byHash.get(c.configHash);
    const measured = !!r;
    const ping = measured ? r!.pingMs : -1;
    const ok = measured && ping > 0;
    const testing = !measured && testingSet.has(c.configHash);

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
      avg_ping: measured ? (ok ? ping : -1) : testing ? -2 : -1,
      measured,
      raw: c.raw,
      uuid: c.uuid,
      path: c.path,
      sni: c.sni,
      tags: c.tags,
      operator_pings: [],
      updated_at: new Date(r?.measuredAt ?? Date.now()).toISOString(),
    };
  });

  rows.sort((a, b) => {
    const aDone = !!a.measured;
    const bDone = !!b.measured;

    const tier = (x: RankedConfig, done: boolean) => {
      if (done && x.success_rate > 0) return 0;
      if (done) return 1;
      if (x.avg_ping === -2) return 2;
      return 3;
    };
    const ta = tier(a, aDone);
    const tb = tier(b, bDone);
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
    measured: false,
    raw: c.raw,
    uuid: c.uuid,
    path: c.path,
    sni: c.sni,
    tags: c.tags,
    operator_pings: [],
    updated_at: new Date().toISOString(),
  }));
}
