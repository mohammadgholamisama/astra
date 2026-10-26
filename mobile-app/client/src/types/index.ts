export type Operator = 'irancell' | 'mci' | 'rightel';

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
