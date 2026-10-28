import { NativeModules, Platform } from 'react-native';

type NativeXrayPing = {
  pingRealDelay: (shareLink: string, timeoutMs: number) => Promise<number>;
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
 */
export async function pingRealDelay(
  shareLink: string,
  timeoutMs = 12_000,
): Promise<number> {
  if (Platform.OS === 'android' && Native?.pingRealDelay) {
    try {
      return normalize(await Native.pingRealDelay(shareLink, timeoutMs));
    } catch {
      return -1;
    }
  }
  return -1;
}

/** Short download+upload burst through the config (kbps). */
export async function measureSpeed(
  shareLink: string,
  downloadBudgetMs = 1600,
  uploadBudgetMs = 1400,
): Promise<{ downloadKbps: number; uploadKbps: number }> {
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

/** Packet loss % (0–100) via N short HTTP probes through the config. */
export async function measurePacketLoss(
  shareLink: string,
  probes = 10,
  perProbeTimeoutMs = 1500,
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
      return {
        lossPct: loss < 0 ? -1 : Math.min(100, Math.max(0, Math.round(loss * 10) / 10)),
        sent: Number(r?.sent) || 0,
        ok: Number(r?.ok) || 0,
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
