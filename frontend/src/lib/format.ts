/** Persian byte formatting for traffic display. */
export function formatTraffic(bytes: number, opts?: { empty?: string }): string {
  const n = Number(bytes) || 0;
  if (n <= 0) return opts?.empty ?? "۰";
  const units = ["بایت", "کیلوبایت", "مگابایت", "گیگابایت", "ترابایت"];
  let x = n;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  const num = x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(2);
  return `${toFaDigits(num)} ${units[i]}`;
}

/** Data limit: 0 / null → نامحدود */
export function formatDataLimit(bytes: number | null | undefined): string {
  const n = Number(bytes) || 0;
  if (n <= 0) return "نامحدود";
  return formatTraffic(n);
}

/** Usage ratio 0–100; null when unlimited. */
export function usagePercent(used: number, limit: number): number | null {
  const lim = Number(limit) || 0;
  if (lim <= 0) return null;
  const u = Math.max(0, Number(used) || 0);
  return Math.min(100, Math.round((u / lim) * 1000) / 10);
}

export function remainingBytes(used: number, limit: number): number | null {
  const lim = Number(limit) || 0;
  if (lim <= 0) return null;
  return Math.max(0, lim - (Number(used) || 0));
}

/** Unix expire → human Persian label + urgency. */
export function formatExpire(expire: number | null | undefined): {
  label: string;
  daysLeft: number | null;
  tone: "ok" | "warn" | "danger" | "muted";
} {
  const ts = Number(expire) || 0;
  if (ts <= 0) {
    return { label: "بدون انقضا", daysLeft: null, tone: "muted" };
  }
  const now = Math.floor(Date.now() / 1000);
  const diff = ts - now;
  if (diff <= 0) {
    return { label: "منقضی شده", daysLeft: 0, tone: "danger" };
  }
  const days = Math.ceil(diff / 86400);
  const hours = Math.ceil(diff / 3600);
  let label: string;
  if (days >= 2) label = `${toFaDigits(days)} روز مانده`;
  else if (hours >= 2) label = `${toFaDigits(hours)} ساعت مانده`;
  else label = "کمتر از ۲ ساعت";
  const tone = days <= 3 ? "danger" : days <= 7 ? "warn" : "ok";
  return { label, daysLeft: days, tone };
}

export function toFaDigits(input: string | number): string {
  return String(input).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);
}
