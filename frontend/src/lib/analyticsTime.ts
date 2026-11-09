/** Browser timezone helpers for analytics charts/labels. */

export function tzOffsetMinutes(): number {
  return new Date().getTimezoneOffset();
}

/** Hour bucket label in the viewer's local timezone. */
export function hourLabelLocal(ts: number): string {
  return new Date(ts * 1000).toLocaleString("fa-IR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Clock hour 0–23 in the viewer's local timezone. */
export function localHourOf(ts: number): number {
  return new Date(ts * 1000).getHours();
}

export function buildLocalHod(
  points: { hour_ts: number; raw_bytes: number }[]
): { bytes: number[]; peak_hour: number | null; peak_bytes: number } {
  const bytes = Array.from({ length: 24 }, () => 0);
  for (const p of points) {
    const i = localHourOf(p.hour_ts);
    if (i >= 0 && i < 24) bytes[i] += Number(p.raw_bytes || 0);
  }
  if (!bytes.some((b) => b > 0)) {
    return { bytes, peak_hour: null, peak_bytes: 0 };
  }
  let peak_hour = 0;
  for (let i = 1; i < 24; i += 1) {
    if (bytes[i] > bytes[peak_hour]) peak_hour = i;
  }
  return { bytes, peak_hour, peak_bytes: bytes[peak_hour] };
}

export function formatLocalClock(hour: number): string {
  const h = ((Math.floor(hour) % 24) + 24) % 24;
  return `${String(h).padStart(2, "0")}:00`;
}
