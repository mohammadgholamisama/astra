/** Compute avg / jitter / stability from Real-Delay samples (ms; ≤0 = fail). */

export type LatencyStats = {
  pingMs: number;
  jitterMs: number;
  stabilityPct: number;
};

export function computeLatencyStats(samples: number[]): LatencyStats {
  const ok = samples.filter((v) => typeof v === 'number' && v > 0);
  const n = samples.length;
  if (n <= 0) {
    return { pingMs: -1, jitterMs: -1, stabilityPct: -1 };
  }
  const stabilityPct = Math.round((ok.length / n) * 1000) / 10;
  if (ok.length === 0) {
    return { pingMs: -1, jitterMs: -1, stabilityPct };
  }
  const avg = ok.reduce((s, v) => s + v, 0) / ok.length;
  const pingMs = Math.round(avg);
  if (ok.length === 1) {
    return { pingMs, jitterMs: 0, stabilityPct };
  }
  const mad =
    ok.reduce((s, v) => s + Math.abs(v - avg), 0) / ok.length;
  const jitterMs = Math.round(mad);
  return { pingMs, jitterMs, stabilityPct };
}
