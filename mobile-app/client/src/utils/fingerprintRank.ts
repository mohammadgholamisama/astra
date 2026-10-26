import type { RankedConfig } from '../types';

export type FingerprintRankItem = {
  host: string;
  port: number;
  match_host: string;
  label?: string;
  avg_ping: number;
  sample_count: number;
  device_count: number;
};

/**
 * Build panel order payload from the live page ranking.
 * - Only measured + successful pings (avg_ping > 0)
 * - Grouped by host:port:match_host (domain/SNI)
 * - Averaged when several configs share the same fingerprint
 * - Sorted best-first
 */
export function buildFingerprintRanksFromPage(
  ranked: RankedConfig[],
): FingerprintRankItem[] {
  type Acc = {
    host: string;
    port: number;
    match_host: string;
    label: string;
    sum: number;
    n: number;
  };
  const map = new Map<string, Acc>();

  for (const row of ranked) {
    if (!row.measured || !(row.avg_ping > 0)) continue;
    const host = String(row.host || '')
      .trim()
      .toLowerCase()
      .replace(/^www\./, '');
    const port = Number(row.port) || 0;
    if (!host || port < 1 || port > 65535) continue;

    const match = String(row.match_host || row.sni || '')
      .trim()
      .toLowerCase()
      .replace(/^www\./, '');

    const key = `${host}|${port}|${match}`;
    const prev = map.get(key);
    if (!prev) {
      map.set(key, {
        host,
        port,
        match_host: match,
        label: (row.remark || '').trim().slice(0, 128),
        sum: row.avg_ping,
        n: 1,
      });
    } else {
      prev.sum += row.avg_ping;
      prev.n += 1;
      if (!prev.label && row.remark) {
        prev.label = row.remark.trim().slice(0, 128);
      }
    }
  }

  const items: FingerprintRankItem[] = [];
  for (const acc of map.values()) {
    const avg = Math.round((acc.sum / acc.n) * 10) / 10;
    items.push({
      host: acc.host,
      port: acc.port,
      match_host: acc.match_host,
      label: acc.label || undefined,
      avg_ping: avg,
      sample_count: acc.n,
      device_count: 1,
    });
  }

  items.sort((a, b) => {
    if (a.avg_ping !== b.avg_ping) return a.avg_ping - b.avg_ping;
    if (a.host !== b.host) return a.host.localeCompare(b.host);
    return a.port - b.port;
  });
  return items;
}
