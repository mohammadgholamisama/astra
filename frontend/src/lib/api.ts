const API_BASE = import.meta.env.VITE_API_BASE || "";

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function authHeaders(): HeadersInit {
  const token = localStorage.getItem("astra_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Parse FastAPI `detail` (string | object | validation array). */
function parseApiDetail(body: unknown): { message: string; code?: string } {
  if (!body || typeof body !== "object") {
    return { message: "خطای ناشناخته" };
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return { message: detail };
  }
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (item && typeof item === "object" && "msg" in item) {
        return String((item as { msg: unknown }).msg);
      }
      return String(item);
    });
    return { message: parts.filter(Boolean).join("؛ ") || "داده نامعتبر است" };
  }
  if (detail && typeof detail === "object") {
    const d = detail as { message?: unknown; msg?: unknown; code?: unknown };
    const message = String(d.message || d.msg || "خطای درخواست");
    const code = d.code != null ? String(d.code) : undefined;
    return { message, code };
  }
  return { message: "خطای درخواست" };
}

type ApiOptions = RequestInit & { timeoutMs?: number };

export async function api<T>(
  path: string,
  options: ApiOptions = {}
): Promise<T> {
  const { timeoutMs, ...fetchInit } = options;
  const headers: HeadersInit = {
    ...(fetchInit.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...authHeaders(),
    ...(fetchInit.headers || {}),
  };

  const controller =
    typeof timeoutMs === "number" && timeoutMs > 0 ? new AbortController() : null;
  const timer = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...fetchInit,
      headers,
      signal: controller?.signal ?? fetchInit.signal,
    });
  } catch (e) {
    if (timer != null) window.clearTimeout(timer);
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError(504, "پاسخ از سرور نرسید (تایم‌اوت) — دوباره تلاش کنید");
    }
    throw e;
  }
  if (timer != null) window.clearTimeout(timer);
  if (!res.ok) {
    let message = res.statusText || "خطای درخواست";
    let code: string | undefined;
    try {
      const j = await res.json();
      const parsed = parseApiDetail(j);
      message = parsed.message;
      code = parsed.code;
    } catch {
      if (res.status === 502 || res.status === 504) {
        message = "پاسخ از سرور نرسید (تایم‌اوت) — دوباره تلاش کنید";
      } else if (res.status >= 500) {
        message = `خطای سرور (${res.status})`;
      } else if (!message || message === "Error") {
        message = "خطای درخواست";
      }
    }
    // Session expired — force re-login (avoid confusing remote-panel auth errors)
    if (res.status === 401 && !path.includes("/api/admin/token")) {
      localStorage.removeItem("astra_token");
      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        window.location.assign("/login");
      }
    }
    throw new ApiError(res.status, message, code);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export async function login(username: string, password: string) {
  const body = new URLSearchParams();
  body.set("username", username);
  body.set("password", password);
  const res = await fetch(`${API_BASE}/api/admin/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new ApiError(res.status, "نام کاربری یا رمز اشتباه است");
  return res.json() as Promise<{ access_token: string; token_type: string }>;
}

export type PanelType = "sanaei" | "pasarguard" | "marzban" | "rebecca" | "astra";

export interface AstraOutbound {
  id: number;
  panel_id: number;
  uri: string;
  remark: string;
  protocol: string;
  host: string;
  port: number;
  is_enabled: boolean;
  last_ping_ms: number | null;
  last_ping_ok: boolean | null;
  last_ping_at: string | null;
  sort_order: number;
  created_at?: string | null;
}

export interface AstraNodeClient {
  id: number;
  panel_id: number;
  email: string;
  uuid: string;
  enable: boolean;
  data_limit: number;
  used_traffic: number;
  weighted_used: number;
  coefficient: number;
  limit_ip: number;
  expire: number;
  on_hold_expire_duration?: number;
  links_count: number;
  share_links?: string[];
  subscription_url?: string | null;
  user_id: number | null;
  astra_username: string | null;
  user_status: string | null;
  group_id: number | null;
  group_name: string | null;
  account_status: string | null;
  account_used_traffic: number | null;
  created_at?: string | null;
}

export interface Panel {
  id: number;
  name: string;
  type: PanelType;
  base_url: string;
  username: string | null;
  has_password: boolean;
  has_token: boolean;
  meta: Record<string, unknown>;
  is_active: boolean;
  last_ok_at: string | null;
  created_at: string;
}

export interface GroupPanel {
  panel_id: number;
  config: Record<string, unknown>;
  priority: number;
  panel_name?: string | null;
  panel_type?: string | null;
  panel_is_active?: boolean;
}

export interface Group {
  id: number;
  name: string;
  description: string | null;
  is_disabled: boolean;
  /** Optional text in real config remarks for this group's panels: `#1. TAG 2.5G | name` */
  config_tag?: string;
  /** Optional info messages prepended as first configs in subscription */
  notices?: string[];
  /** null = default bucket «همه گروه‌ها» */
  category_id?: number | null;
  panels: GroupPanel[];
  created_at: string;
}

export interface GroupCategory {
  id: number;
  name: string;
  sort_order: number;
  groups_count: number;
  created_at: string;
}

export interface PanelAccount {
  id: number;
  panel_id: number;
  /** Astra sales group this remote account belongs to (per-group coeffs). */
  group_id?: number | null;
  panel_name?: string | null;
  panel_type?: string | null;
  panel_base_url?: string | null;
  remote_username: string | null;
  status: string;
  links: string[];
  error_message: string | null;
  used_traffic: number;
  /** Bytes already locked before the current coefficient period */
  coeff_anchor_raw?: number;
  coeff_anchor_weighted?: number;
  applied_coefficient?: number | null;
  last_sync_at: string | null;
  configs_count?: number;
}

export interface User {
  username: string;
  display_name?: string | null;
  status: string;
  used_traffic: number;
  lifetime_used_traffic: number;
  data_limit: number;
  expire: number | null;
  on_hold_expire_duration?: number;
  note: string | null;
  subscription_url: string;
  links: string[];
  group_ids: number[];
  panel_accounts: PanelAccount[];
  created_at?: string;
}

export interface TopUserStat {
  username: string;
  display_name: string | null;
  used_traffic: number;
  data_limit: number;
  status: string;
  expire: number;
}

export interface SystemStats {
  version: string;
  panel: string;
  users_total: number;
  users_active: number;
  users_expired?: number;
  users_limited?: number;
  users_with_errors?: number;
  panels_total: number;
  panels_active: number;
  groups_total: number;
  sync_errors: number;
  total_used_traffic: number;
  top_users?: TopUserStat[];
  update_supported?: boolean;
}

export interface HostBytesStat {
  total: number;
  used: number;
  free: number;
  percent: number;
  available?: number;
}

export interface HostDiskStat extends HostBytesStat {
  mount: string;
}

export interface HostServiceStat {
  name: string;
  label: string;
  state: string;
}

export interface HostMetrics {
  ok: boolean;
  engine: string;
  hostname: string;
  os: string;
  arch: string;
  python: string;
  collected_at: number;
  cpu_percent: number;
  cpu_count_logical: number;
  cpu_count_physical?: number | null;
  cpu_per_core: number[];
  cpu_freq_mhz?: number | null;
  load_avg: [number, number, number] | number[];
  memory: HostBytesStat;
  swap: HostBytesStat;
  disks: HostDiskStat[];
  uptime_seconds: number;
  boot_time: number;
  process_count: number;
  net: {
    bytes_sent: number;
    bytes_recv: number;
    packets_sent: number;
    packets_recv: number;
  };
  services: HostServiceStat[];
  note?: string;
}

export interface UpdateStatus {
  available: boolean;
  check_ok?: boolean;
  update_available: boolean;
  local_version: string;
  remote_version: string;
  git_behind?: boolean;
  message: string;
}

export interface ApiKey {
  id: number;
  name: string;
  key_prefix: string;
  note: string | null;
  is_active: boolean;
  expires_at: string | null;
  created_at: string;
  key?: string;
}

export function formatBytes(n: number): string {
  if (!n) return "∞";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let x = n;
  for (const unit of u) {
    if (x < 1024) return `${x.toFixed(1)} ${unit}`;
    x /= 1024;
  }
  return `${x.toFixed(1)} PB`;
}

export function gbToBytes(gb: number): number {
  return Math.round(gb * 1024 ** 3);
}

export function bytesToGbInput(bytes: number | null | undefined): string {
  const n = Number(bytes) || 0;
  if (n <= 0) return "0";
  const gb = n / 1024 ** 3;
  if (gb >= 100) return String(Math.round(gb));
  const rounded = Math.round(gb * 1000) / 1000;
  return String(rounded);
}

/** Remaining whole days until expire; 0 if unlimited or already expired. */
export function expireToDaysInput(expire: number | null | undefined): string {
  const ts = Number(expire) || 0;
  if (ts <= 0) return "0";
  const now = Math.floor(Date.now() / 1000);
  if (ts <= now) return "0";
  return String(Math.ceil((ts - now) / 86400));
}
