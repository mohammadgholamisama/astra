import type {
  Operator,
  ParsedConfig,
  PingSamplePayload,
  RankedListResponse,
  LocalPingResult,
} from '../types';

// Production hub behind panel nginx. Override via settings.apiBaseUrl if needed.
// Emulator can still reach it over the public URL.
export const DEFAULT_BASE = 'https://p.vokqem.ir/ping-api';
/** Must match astra-ping-hub API_KEY on the panel server. */
export const DEFAULT_API_KEY = 'hub_82e049ca9f786e9c1b0e2169603a2e654561be45c8def7e2';

export interface ApiConfig {
  baseUrl: string;
  apiKey: string;
  deviceId: string;
}

function joinUrl(base: string, path: string): string {
  return `${base.replace(/\/$/, '')}${path.startsWith('/') ? path : `/${path}`}`;
}

async function request<T>(
  cfg: ApiConfig,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(joinUrl(cfg.baseUrl, path), {
    ...init,
    signal: init.signal,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'X-API-Key': cfg.apiKey,
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { raw: text };
  }
  if (!res.ok) {
    const err = (body as { error?: string })?.error || `HTTP ${res.status}`;
    throw new Error(err);
  }
  return body as T;
}

export function buildPingSamples(
  operator: Operator,
  configs: ParsedConfig[],
  results: LocalPingResult[],
  deviceId: string,
): PingSamplePayload[] {
  const byHash = new Map(results.map((r) => [r.configHash, r]));
  return configs.map((c) => {
    const r = byHash.get(c.configHash);
    return {
      config_hash: c.configHash,
      operator,
      ping_ms: r?.pingMs ?? -1,
      download_kbps:
        r?.downloadKbps != null && r.downloadKbps > 0 ? r.downloadKbps : undefined,
      upload_kbps:
        r?.uploadKbps != null && r.uploadKbps > 0 ? r.uploadKbps : undefined,
      remark: c.remark,
      protocol: c.protocol,
      transport: c.transport,
      security: c.security,
      host: c.address,
      port: c.port,
      match_host: c.matchHost,
      device_id: deviceId,
      measured_at: r?.measuredAt ?? Date.now(),
    };
  });
}

export async function submitPingBatch(
  cfg: ApiConfig,
  operator: Operator,
  samples: PingSamplePayload[],
): Promise<{ ok: boolean; accepted: number; ranked: RankedListResponse }> {
  return request(cfg, '/api/v1/pings', {
    method: 'POST',
    body: JSON.stringify({
      operator,
      device_id: cfg.deviceId,
      samples,
    }),
  });
}

export async function fetchRanked(cfg: ApiConfig): Promise<RankedListResponse> {
  return request(cfg, '/api/v1/ranked');
}

export type FingerprintRankPayload = {
  host: string;
  port: number;
  match_host: string;
  label?: string;
  avg_ping: number;
  download_kbps?: number;
  upload_kbps?: number;
  sample_count?: number;
  device_count?: number;
};

/** Reorder Astra config-order. Prefer `items` from the live page (fast + exact). */
export async function syncPanelRanks(
  cfg: ApiConfig,
  signal?: AbortSignal,
  items?: FingerprintRankPayload[],
): Promise<{ ok: boolean; count: number; message?: string; source?: string }> {
  const body =
    items && items.length > 0 ? JSON.stringify({ items }) : '{}';
  return request(cfg, '/api/v1/panel/sync', {
    method: 'POST',
    body,
    signal,
  });
}

export function openRankedSocket(
  cfg: ApiConfig,
  onMessage: (list: RankedListResponse) => void,
  onError?: (err: Event) => void,
): WebSocket {
  const httpBase = cfg.baseUrl.replace(/\/$/, '');
  const wsBase = httpBase.replace(/^http/, 'ws');
  const url = `${wsBase}/ws/ranked?api_key=${encodeURIComponent(cfg.apiKey)}`;
  const ws = new WebSocket(url);
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(String(ev.data)) as {
        type?: string;
        payload?: RankedListResponse;
      };
      if (data.type === 'ranked' && data.payload) {
        onMessage(data.payload);
      }
    } catch {
      /* ignore malformed */
    }
  };
  ws.onerror = (e) => onError?.(e);
  return ws;
}
