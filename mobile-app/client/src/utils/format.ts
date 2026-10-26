export function toFaDigits(input: string | number): string {
  const map = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];
  return String(input).replace(/\d/g, (d) => map[Number(d)] ?? d);
}

export function formatMs(ms: number): string {
  if (ms === -2) return '…';
  if (ms < 0) return '—';
  return `${toFaDigits(Math.round(ms))}ms`;
}

export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${toFaDigits(m)}:${toFaDigits(String(s).padStart(2, '0'))}`;
}

export function pingTone(ms: number): string {
  if (ms < 0) return 'text-neon-rose';
  if (ms < 150) return 'text-neon-mint';
  if (ms < 300) return 'text-neon-cyan';
  if (ms < 500) return 'text-neon-amber';
  return 'text-neon-rose';
}
