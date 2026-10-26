import type {
  Operator,
  ParsedConfig,
  PingSamplePayload,
  RankedListResponse,
  LocalPingResult,
} from '../types';

// Emulator → host loopback; release builds → panel nginx /ping-api (override in settings).
const DEFAULT_BASE = __DEV__
  ? 'http://10.0.2.2:18080'
  : 'https://p.vokqem.ir/ping-api';

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

export { DEFAULT_BASE };
