import { NativeModules, Platform } from 'react-native';

type NativeXrayPing = {
  pingRealDelay: (shareLink: string, timeoutMs: number) => Promise<number>;
  pingTcp: (host: string, port: number, timeoutMs: number) => Promise<number>;
  measureSpeed?: (
    shareLink: string,
    downloadBudgetMs: number,
    uploadBudgetMs: number,
  ) => Promise<{ download_kbps?: number; upload_kbps?: number }>;
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
  timeoutMs = 2200,
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

export async function isXrayNativeAvailable(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.isAvailable) return false;
  try {
    return Boolean(await Native.isAvailable());
  } catch {
    return false;
  }
}
