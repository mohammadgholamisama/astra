import { NativeModules, Platform } from 'react-native';

type NativeXrayPing = {
  pingRealDelay: (shareLink: string, timeoutMs: number) => Promise<number>;
  /** Real Delay to one fixed URL (stability destinations). */
  pingRealDelayUrl?: (
    shareLink: string,
    destUrl: string,
    timeoutMs: number,
  ) => Promise<number>;
  pingTcp: (host: string, port: number, timeoutMs: number) => Promise<number>;
  measureSpeed?: (
    shareLink: string,
    downloadBudgetMs: number,
    uploadBudgetMs: number,
  ) => Promise<{ download_kbps?: number; upload_kbps?: number }>;
  measurePacketLoss?: (
    shareLink: string,
    probes: number,
    perProbeTimeoutMs: number,
  ) => Promise<{ loss_pct?: number; sent?: number; ok?: number }>;
  isAvailable: () => Promise<boolean>;
  cancelAllPings?: () => Promise<boolean>;
};

const Native = NativeModules.XrayPing as NativeXrayPing | undefined;

function normalize(ms: unknown): number {
  if (typeof ms !== 'number' || Number.isNaN(ms)) return -1;
  return ms < 0 ? -1 : Math.round(ms);
}

function normalizeKbps(v: unknown): number {
  if (typeof v !== 'number' || Number.isNaN(v) || v <= 0) return -1;
  return Math.round(v * 10) / 10;
}

/**
 * Fire-and-forget hard stop. Never await — bridge must stay free for UI.
 */
export function cancelAllPings(): void {
  if (Platform.OS === 'android' && Native?.cancelAllPings) {
    try {
      const p = Native.cancelAllPings();
      if (p && typeof (p as Promise<boolean>).then === 'function') {
        void (p as Promise<boolean>).catch(() => undefined);
      }
    } catch {
      /* ignore */
    }
  }
}

/** TCP connect only — quick reachability, NOT real proxy delay. */
export async function pingTcp(
  host: string,
  port: number,
  timeoutMs = 3000,
): Promise<number> {
  if (Platform.OS === 'android' && Native?.pingTcp) {
    try {
      return normalize(await Native.pingTcp(host, port, timeoutMs));
    } catch {
      return -1;
    }
  }
  return -1;
}

/**
 * Real Xray outbound delay (Reality / VLESS / …) via LibXray.
 * Optional destUrl pins the probe to one host (stability multi-destination).
 */
export async function pingRealDelay(
  shareLink: string,
  timeoutMs = 12_000,
  destUrl?: string,
): Promise<number> {
  if (Platform.OS !== 'android' || !Native) return -1;
  try {
    const url = destUrl?.trim();
    if (url && Native.pingRealDelayUrl) {
      return normalize(await Native.pingRealDelayUrl(shareLink, url, timeoutMs));
    }
    if (Native.pingRealDelay) {
      return normalize(await Native.pingRealDelay(shareLink, timeoutMs));
    }
  } catch {
    return -1;
  }
  return -1;
}

/** Short download+upload burst through the config (kbps). Pass 0 to skip a direction. */
export async function measureSpeed(
  shareLink: string,
  downloadBudgetMs = 1600,
  uploadBudgetMs = 1400,
): Promise<{ downloadKbps: number; uploadKbps: number }> {
  if (downloadBudgetMs <= 0 && uploadBudgetMs <= 0) {
    return { downloadKbps: -1, uploadKbps: -1 };
  }
  if (Platform.OS === 'android' && Native?.measureSpeed) {
    try {
      const r = await Native.measureSpeed(
        shareLink,
        downloadBudgetMs,
        uploadBudgetMs,
      );
      return {
        downloadKbps: normalizeKbps(r?.download_kbps),
        uploadKbps: normalizeKbps(r?.upload_kbps),
      };
    } catch {
      return { downloadKbps: -1, uploadKbps: -1 };
    }
  }
  return { downloadKbps: -1, uploadKbps: -1 };
}

/**
 * Packet loss % (0–100) via N short HTTP probes through the config.
 * Default 100 probes → 1% resolution (e.g. 97% stays 97, not 90/100).
 */
export async function measurePacketLoss(
  shareLink: string,
  probes = 100,
  perProbeTimeoutMs = 800,
): Promise<{ lossPct: number; sent: number; ok: number }> {
  if (Platform.OS === 'android' && Native?.measurePacketLoss) {
    try {
      const r = await Native.measurePacketLoss(
        shareLink,
        probes,
        perProbeTimeoutMs,
      );
      const loss =
        typeof r?.loss_pct === 'number' && Number.isFinite(r.loss_pct)
          ? r.loss_pct
          : -1;
      if (loss < 0) return { lossPct: -1, sent: 0, ok: 0 };
      const sent = Number(r?.sent) || 0;
      const ok = Number(r?.ok) || 0;
      // Prefer exact ratio from counts when available
      const fromCounts =
        sent > 0 ? ((sent - ok) / sent) * 100 : loss;
      const precise = Math.round(fromCounts * 10) / 10;
      return {
        lossPct: Math.min(100, Math.max(0, precise)),
        sent,
        ok,
      };
    } catch {
      return { lossPct: -1, sent: 0, ok: 0 };
    }
  }
  return { lossPct: -1, sent: 0, ok: 0 };
}

export async function isXrayNativeAvailable(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.isAvailable) return false;
  try {
    return Boolean(await Native.isAvailable());
  } catch {
    return false;
  }
}
