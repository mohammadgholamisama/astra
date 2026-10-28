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
  packet_loss_pct?: number;
  has_packet_loss?: boolean;
  sample_count: number;
  device_count: number;
};

function rowOk(row: RankedConfig): boolean {
  if (!row.measured) return false;
  if (row.avg_ping > 0) return true;
  if ((row.download_kbps ?? 0) > 0 || (row.upload_kbps ?? 0) > 0) return true;
  if (
    row.packet_loss_pct != null &&
    row.packet_loss_pct >= 0 &&
    row.packet_loss_pct < 100
  ) {
    return true;
  }
  if (row.success_rate > 0) return true;
  return false;
}

/**
 * Build panel order payload from the live page ranking.
 * Includes ping / speed / packet successes.
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
    lossSum: number;
    lossN: number;
    samples: number;
  };
  const map = new Map<string, Acc>();

  for (const row of ranked) {
    if (!rowOk(row)) continue;
    const host = String(row.host || '')
      .trim()
      .toLowerCase()
      .replace(/^www\./, '');
    const port = Number(row.port) || 0;
    if (!host || port < 1 || port > 65535) continue;

    let match = String(row.match_host || row.sni || '')
      .trim()
      .toLowerCase()
      .replace(/^www\./, '');
    if (match.endsWith('.')) match = match.slice(0, -1);

    const key = `${host}|${port}|${match}`;
    const prev = map.get(key);
    const dl = row.download_kbps ?? 0;
    const ul = row.upload_kbps ?? 0;
    const ping = row.avg_ping > 0 ? row.avg_ping : 0;
    const loss =
      row.packet_loss_pct != null && row.packet_loss_pct >= 0
        ? row.packet_loss_pct
        : -1;
    if (!prev) {
      map.set(key, {
        host,
        port,
        match_host: match,
        label: (row.remark || '').trim().slice(0, 128),
        pingSum: ping,
        pingN: ping > 0 ? 1 : 0,
        dlSum: dl > 0 ? dl : 0,
        dlN: dl > 0 ? 1 : 0,
        ulSum: ul > 0 ? ul : 0,
        ulN: ul > 0 ? 1 : 0,
        lossSum: loss >= 0 ? loss : 0,
        lossN: loss >= 0 ? 1 : 0,
        samples: 1,
      });
    } else {
      prev.samples += 1;
      if (ping > 0) {
        prev.pingSum += ping;
        prev.pingN += 1;
      }
      if (dl > 0) {
        prev.dlSum += dl;
        prev.dlN += 1;
      }
      if (ul > 0) {
        prev.ulSum += ul;
        prev.ulN += 1;
      }
      if (loss >= 0) {
        prev.lossSum += loss;
        prev.lossN += 1;
      }
      if (!prev.label && row.remark) {
        prev.label = row.remark.trim().slice(0, 128);
      }
    }
  }

  const items: FingerprintRankItem[] = [];
  for (const acc of map.values()) {
    const avg =
      acc.pingN > 0
        ? Math.round((acc.pingSum / acc.pingN) * 10) / 10
        : 0;
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
      packet_loss_pct:
        acc.lossN > 0
          ? Math.round((acc.lossSum / acc.lossN) * 10) / 10
          : undefined,
      has_packet_loss: acc.lossN > 0 ? true : undefined,
      sample_count: acc.samples,
      device_count: 1,
    });
  }

  return arrangeByCategorySlots(items);
}
