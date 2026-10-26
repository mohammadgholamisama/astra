import { NativeModules, Platform } from 'react-native';

type NativeXrayPing = {
  pingRealDelay: (shareLink: string, timeoutMs: number) => Promise<number>;
  pingTcp: (host: string, port: number, timeoutMs: number) => Promise<number>;
  isAvailable: () => Promise<boolean>;
  cancelAllPings?: () => Promise<boolean>;
};

const Native = NativeModules.XrayPing as NativeXrayPing | undefined;

function normalize(ms: unknown): number {
  if (typeof ms !== 'number' || Number.isNaN(ms)) return -1;
  return ms < 0 ? -1 : Math.round(ms);
}

/** Hard-stop every native ping (pool drain). Safe to call often. */
export async function cancelAllPings(): Promise<void> {
  if (Platform.OS === 'android' && Native?.cancelAllPings) {
    try {
      await Native.cancelAllPings();
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
  timeoutMs = 8000,
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

export async function isXrayNativeAvailable(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.isAvailable) return false;
  try {
    return Boolean(await Native.isAvailable());
  } catch {
    return false;
  }
}
