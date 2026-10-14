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

export async function api<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: HeadersInit = {
    ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...authHeaders(),
    ...(options.headers || {}),
  };

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
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

export type PanelType = "sanaei" | "pasarguard" | "marzban" | "rebecca";

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
}

export interface Group {
  id: number;
  name: string;
  description: string | null;
  is_disabled: boolean;
  /** Optional info messages prepended as first configs in subscription */
  notices?: string[];
  panels: GroupPanel[];
  created_at: string;
}

export interface PanelAccount {
  id: number;
  panel_id: number;
  panel_name?: string | null;
  panel_type?: string | null;
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
}

export interface User {
  username: string;
  display_name?: string | null;
  status: string;
  used_traffic: number;
  lifetime_used_traffic: number;
  data_limit: number;
  expire: number | null;
  note: string | null;
  subscription_url: string;
  links: string[];
  group_ids: number[];
  panel_accounts: PanelAccount[];
  created_at?: string;
}

export interface SystemStats {
  version: string;
  panel: string;
  users_total: number;
  users_active: number;
  panels_total: number;
  panels_active: number;
  groups_total: number;
  sync_errors: number;
  total_used_traffic: number;
  update_supported?: boolean;
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
