import {
  NativeEventEmitter,
  NativeModules,
  Platform,
} from 'react-native';

type NativeVpn = {
  isVpnActive: () => Promise<boolean>;
  startWatching: () => Promise<boolean>;
  stopWatching: () => Promise<boolean>;
  VPN_EVENT?: string;
};

const Native = NativeModules.NetworkGuard as NativeVpn | undefined;
const EVENT = Native?.VPN_EVENT || 'vpnStateChanged';

/** True when any system VPN / tunnel interface is up. */
export async function isDeviceVpnActive(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.isVpnActive) return false;
  try {
    return Boolean(await Native.isVpnActive());
  } catch {
    return false;
  }
}

/**
 * Subscribe to VPN up/down. Returns unsubscribe.
 * onChange only fires when the active flag actually changes.
 */
export function watchDeviceVpn(onChange: (active: boolean) => void): () => void {
  if (Platform.OS !== 'android' || !Native?.startWatching) {
    return () => undefined;
  }

  let alive = true;
  let last: boolean | null = null;
  const push = (active: boolean) => {
    if (!alive || last === active) return;
    last = active;
    onChange(active);
  };

  const emitter = new NativeEventEmitter(NativeModules.NetworkGuard);
  const sub = emitter.addListener(EVENT, (payload: { active?: boolean }) => {
    push(Boolean(payload?.active));
  });

  void Native.startWatching().catch(() => undefined);
  void isDeviceVpnActive().then(push);

  // Poll fallback — some OEM VPN toggles miss NetworkCallback
  const poll = setInterval(() => {
    if (!alive) return;
    void isDeviceVpnActive().then(push);
  }, 2000);

  return () => {
    alive = false;
    sub.remove();
    clearInterval(poll);
    void Native.stopWatching?.().catch(() => undefined);
  };
}
