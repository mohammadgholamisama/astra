export function toFaDigits(input: string | number): string {
  const map = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];
  return String(input).replace(/\d/g, (d) => map[Number(d)] ?? d);
}

export function formatMs(ms: number): string {
  if (ms === -2) return '…';
  if (ms < 0) return '—';
  return `${toFaDigits(Math.round(ms))}ms`;
}

/** Exact countdown — `M:SS` or `H:MM:SS` when ≥ ۱ ساعت. */
export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = toFaDigits(String(s).padStart(2, '0'));
  if (h > 0) {
    const mm = toFaDigits(String(m).padStart(2, '0'));
    return `${toFaDigits(h)}:${mm}:${ss}`;
  }
  return `${toFaDigits(m)}:${ss}`;
}

export function pingTone(ms: number): string {
  if (ms < 0) return 'text-neon-rose';
  if (ms < 150) return 'text-neon-mint';
  if (ms < 300) return 'text-neon-cyan';
  if (ms < 500) return 'text-neon-amber';
  return 'text-neon-rose';
}
