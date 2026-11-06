export type Operator = 'irancell' | 'mci' | 'rightel';

/** Hub sample tag — no UI picker. */
export const DEFAULT_OPERATOR: Operator = 'irancell';

export const OPERATORS: { id: Operator; label: string; color: string }[] = [
  { id: 'irancell', label: 'ایرانسل', color: '#FFCC00' },
  { id: 'mci', label: 'همراه اول', color: '#00A3E0' },
  { id: 'rightel', label: 'رایتل', color: '#E6007E' },
];

export type Protocol = 'vless' | 'vmess' | 'trojan' | 'ss' | 'unknown';

export interface ParsedConfig {
  raw: string;
  remark: string;
  protocol: Protocol;
  transport: string;
  security: string;
  address: string;
  port: number;
  uuid: string;
  path: string;
  sni: string;
  matchHost: string;
  configHash: string;
  tags: string[];
}

export interface LocalPingResult {
  configHash: string;
  pingMs: number;
  measuredAt: number;
  /** kbps; −1 = not measured / fail */
  downloadKbps?: number;
  /** kbps; −1 = not measured / fail */
  uploadKbps?: number;
  /** 0–100; −1 = not measured / fail */
  packetLossPct?: number;
  /** ms; mean absolute deviation of samples; −1 = n/a */
  jitterMs?: number;
  /** 0–100 success rate across latency samples; −1 = n/a */
  stabilityPct?: number;
}

export interface OperatorPing {
  operator: Operator;
  label_fa: string;
  ping_ms: number;
  updated_at: number;
  has_sample: boolean;
}

export interface RankedConfig {
  rank: number;
  config_hash: string;
  remark: string;
  protocol: string;
  transport: string;
  security: string;
  host: string;
  port: number;
  match_host: string;
  success_rate: number;
  avg_ping: number;
  download_kbps?: number;
  upload_kbps?: number;
  /** 0–100; −1 = fail / unset */
  packet_loss_pct?: number;
  /** ms jitter; −1 / omit = unset */
  jitter_ms?: number;
  /** 0–100 stability; −1 / omit = unset */
  stability_pct?: number;
  /** 0–100 cross-cycle uptime; omit = new / unset */
  uptime_pct?: number;
  /** How many cycles contribute to uptime_pct */
  uptime_samples?: number;
  /** True after a ping attempt finished (success or fail). */
  measured?: boolean;
  raw?: string;
  uuid?: string;
  path?: string;
  sni?: string;
  tags?: string[];
  operator_pings: OperatorPing[];
  updated_at: string;
}

export interface RankedListResponse {
  generated_at: string;
  count: number;
  configs: RankedConfig[];
}

export interface PingSamplePayload {
  config_hash: string;
  operator: Operator;
  ping_ms: number;
  download_kbps?: number;
  upload_kbps?: number;
  packet_loss_pct?: number;
  has_packet_loss?: boolean;
  jitter_ms?: number;
  stability_pct?: number;
  uptime_pct?: number;
  remark?: string;
  protocol?: string;
  transport?: string;
  security?: string;
  host?: string;
  port?: number;
  match_host?: string;
  device_id?: string;
  measured_at?: number;
}
