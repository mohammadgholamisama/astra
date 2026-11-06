import type { LocalPingResult, ParsedConfig, RankedConfig } from '../types';

type LiveMap = Map<string, LocalPingResult>;

function positiveKbps(v: number | undefined): number {
  return v != null && v > 0 ? v : 0;
}

/** Sort key: lower is better. Missing / fail → worst. */
function lossSortKey(v: number | undefined): number {
  if (v == null || v < 0) return 101;
  return v;
}

function jitterSortKey(v: number | undefined): number {
  if (v == null || v < 0) return 1e18;
  return v;
}

/** Higher stability first; missing → last among peers. */
function stabilitySortKey(v: number | undefined): number {
  if (v == null || v < 0) return -1;
  return v;
}

/**
 * In-app list order.
 * Alive = ping>0 OR download/upload>0 OR packet loss measured with <100%
 *   OR stability>0.
 * Rank: stability ↓ → packet loss ↑ → jitter ↑ → ping ↑ → download ↓ → upload ↓
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
    const downloadKbps =
      measured && r!.downloadKbps != null ? r!.downloadKbps : undefined;
    const uploadKbps =
      measured && r!.uploadKbps != null ? r!.uploadKbps : undefined;
    const packetLossPct =
      measured && r!.packetLossPct != null ? r!.packetLossPct : undefined;
    const jitterMs =
      measured && r!.jitterMs != null ? r!.jitterMs : undefined;
    const stabilityPct =
      measured && r!.stabilityPct != null ? r!.stabilityPct : undefined;

    const pingOk = measured && ping > 0;
    const speedOk =
      positiveKbps(downloadKbps) > 0 || positiveKbps(uploadKbps) > 0;
    const packetOk =
      packetLossPct != null && packetLossPct >= 0 && packetLossPct < 100;
    const stabOk = stabilityPct != null && stabilityPct > 0;
    const ok = pingOk || speedOk || packetOk || stabOk;
    const testing = !measured && testingSet.has(c.configHash);
    const avgPing = measured
      ? pingOk
        ? ping
        : ping === 0
          ? 0
          : speedOk || packetOk || stabOk
            ? 0
            : -1
      : testing
        ? -2
        : -1;

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
      avg_ping: avgPing,
      download_kbps: downloadKbps,
      upload_kbps: uploadKbps,
      packet_loss_pct: packetLossPct,
      jitter_ms: jitterMs,
      stability_pct: stabilityPct,
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

  ok.sort((a, b) => {
    const stabA = stabilitySortKey(a.stability_pct);
    const stabB = stabilitySortKey(b.stability_pct);
    if (stabA !== stabB) return stabB - stabA;

    const lossA = lossSortKey(a.packet_loss_pct);
    const lossB = lossSortKey(b.packet_loss_pct);
    if (lossA !== lossB) return lossA - lossB;

    const jitA = jitterSortKey(a.jitter_ms);
    const jitB = jitterSortKey(b.jitter_ms);
    if (jitA !== jitB) return jitA - jitB;

    const pingA = a.avg_ping > 0 ? a.avg_ping : Number.POSITIVE_INFINITY;
    const pingB = b.avg_ping > 0 ? b.avg_ping : Number.POSITIVE_INFINITY;
    if (pingA !== pingB) return pingA - pingB;

    const dlA = positiveKbps(a.download_kbps);
    const dlB = positiveKbps(b.download_kbps);
    if (dlA !== dlB) return dlB - dlA;

    const ulA = positiveKbps(a.upload_kbps);
    const ulB = positiveKbps(b.upload_kbps);
    if (ulA !== ulB) return ulB - ulA;

    return a.remark.localeCompare(b.remark, 'fa');
  });

  const ordered = [...ok, ...testing, ...pending, ...failed];
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
