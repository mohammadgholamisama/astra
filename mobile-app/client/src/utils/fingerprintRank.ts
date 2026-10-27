import type { RankedConfig } from '../types';
import { arrangeByCategorySlots } from './configCategory';

export type FingerprintRankItem = {
  host: string;
  port: number;
  match_host: string;
  label?: string;
  avg_ping: number;
  download_kbps?: number;
  upload_kbps?: number;
  sample_count: number;
  device_count: number;
};

/**
 * Build panel order payload from the live page ranking.
 * Successful pings only; includes speed when measured.
 */
export function buildFingerprintRanksFromPage(
  ranked: RankedConfig[],
): FingerprintRankItem[] {
  type Acc = {
    host: string;
    port: number;
    match_host: string;
    label: string;
    pingSum: number;
    pingN: number;
    dlSum: number;
    dlN: number;
    ulSum: number;
    ulN: number;
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
    const dl = row.download_kbps ?? 0;
    const ul = row.upload_kbps ?? 0;
    if (!prev) {
      map.set(key, {
        host,
        port,
        match_host: match,
        label: (row.remark || '').trim().slice(0, 128),
        pingSum: row.avg_ping,
        pingN: 1,
        dlSum: dl > 0 ? dl : 0,
        dlN: dl > 0 ? 1 : 0,
        ulSum: ul > 0 ? ul : 0,
        ulN: ul > 0 ? 1 : 0,
      });
    } else {
      prev.pingSum += row.avg_ping;
      prev.pingN += 1;
      if (dl > 0) {
        prev.dlSum += dl;
        prev.dlN += 1;
      }
      if (ul > 0) {
        prev.ulSum += ul;
        prev.ulN += 1;
      }
      if (!prev.label && row.remark) {
        prev.label = row.remark.trim().slice(0, 128);
      }
    }
  }

  const items: FingerprintRankItem[] = [];
  for (const acc of map.values()) {
    const avg = Math.round((acc.pingSum / acc.pingN) * 10) / 10;
    items.push({
      host: acc.host,
      port: acc.port,
      match_host: acc.match_host,
      label: acc.label || undefined,
      avg_ping: avg,
      download_kbps:
        acc.dlN > 0 ? Math.round((acc.dlSum / acc.dlN) * 10) / 10 : undefined,
      upload_kbps:
        acc.ulN > 0 ? Math.round((acc.ulSum / acc.ulN) * 10) / 10 : undefined,
      sample_count: acc.pingN,
      device_count: 1,
    });
  }

  return arrangeByCategorySlots(items);
}
