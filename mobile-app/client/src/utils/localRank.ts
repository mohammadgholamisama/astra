import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';
import { arrangeByCategorySlots } from './configCategory';

type LiveMap = Map<string, LocalPingResult>;

/**
 * Live ranking with category slots.
 * When download/upload present → prefer speed; else ping.
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
    const downloadKbps =
      measured && r!.downloadKbps != null && r!.downloadKbps > 0
        ? r!.downloadKbps
        : undefined;
    const uploadKbps =
      measured && r!.uploadKbps != null && r!.uploadKbps > 0
        ? r!.uploadKbps
        : undefined;

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
      download_kbps: downloadKbps,
      upload_kbps: uploadKbps,
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

  const ok = rows.filter((r) => r.measured && r.success_rate > 0);
  const failed = rows.filter((r) => r.measured && r.success_rate <= 0);
  const testing = rows.filter((r) => !r.measured && r.avg_ping === -2);
  const pending = rows.filter((r) => !r.measured && r.avg_ping !== -2);

  const arrangedOk = arrangeByCategorySlots(
    ok.map((r) => ({
      ...r,
      label: r.remark,
      device_count: 1,
      download_kbps: r.download_kbps ?? 0,
      upload_kbps: r.upload_kbps ?? 0,
    })),
  );

  const ordered = [...arrangedOk, ...failed, ...testing, ...pending];
  return ordered.map((row, i) => ({ ...row, rank: i + 1 }));
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
