import { NativeModules, PermissionsAndroid, Platform } from 'react-native';

type NativeBg = {
  startMonitor: (intervalMs: number) => Promise<boolean>;
  stopMonitor: () => Promise<boolean>;
  isMonitoring: () => Promise<boolean>;
  getPermissionStatus: () => Promise<{ notifications: boolean; batteryUnrestricted: boolean }>;
  requestBatteryOptimizationExemption: () => Promise<boolean>;
};

const Native = NativeModules.BackgroundMonitor as NativeBg | undefined;

export type BgPermissionResult = {
  ok: boolean;
  notifications: boolean;
  batteryUnrestricted: boolean;
  message?: string;
};

async function ensureNotifPermission(): Promise<boolean> {
  if (Platform.OS !== 'android') return true;
  if (Platform.Version < 33) return true;
  try {
    const current = await PermissionsAndroid.check(
      PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS,
    );
    if (current) return true;
    const res = await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS,
      {
        title: 'اجازه اعلان',
        message: 'برای پایش پس‌زمینه باید اعلان سرویس فعال باشد.',
        buttonPositive: 'اجازه بده',
        buttonNegative: 'فعلاً نه',
      },
    );
    return res === PermissionsAndroid.RESULTS.GRANTED;
  } catch {
    return false;
  }
}

export async function ensureBackgroundPermissions(): Promise<BgPermissionResult> {
  if (Platform.OS !== 'android' || !Native) {
    return { ok: false, notifications: false, batteryUnrestricted: false, message: 'فقط اندروید' };
  }

  const notifications = await ensureNotifPermission();
  if (!notifications) {
    return {
      ok: false,
      notifications: false,
      batteryUnrestricted: false,
      message: 'اجازه اعلان داده نشد — پایش پس‌زمینه فعال نمی‌شود',
    };
  }

  let batteryUnrestricted = false;
  try {
    const status = await Native.getPermissionStatus();
    batteryUnrestricted = !!status?.batteryUnrestricted;
  } catch {
    batteryUnrestricted = false;
  }

  if (!batteryUnrestricted) {
    try {
      await Native.requestBatteryOptimizationExemption();
      // re-check after system dialog
      const status2 = await Native.getPermissionStatus();
      batteryUnrestricted = !!status2?.batteryUnrestricted;
    } catch {
      /* user may dismiss */
    }
  }

  return {
    ok: notifications,
    notifications,
    batteryUnrestricted,
    message: batteryUnrestricted
      ? undefined
      : 'اعلان OK — برای پایداری بیشتر، محدودیت باتری را هم بردار',
  };
}

export async function startBackgroundMonitor(
  intervalMs = 5 * 60 * 1000,
  opts?: { skipPermissionGate?: boolean },
): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.startMonitor) return false;
  try {
    if (!opts?.skipPermissionGate) {
      const perms = await ensureBackgroundPermissions();
      if (!perms.ok) return false;
    }
    return Boolean(await Native.startMonitor(intervalMs));
  } catch {
    return false;
  }
}

export async function stopBackgroundMonitor(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.stopMonitor) return false;
  try {
    return Boolean(await Native.stopMonitor());
  } catch {
    return false;
  }
}

export async function isBackgroundMonitoring(): Promise<boolean> {
  if (Platform.OS !== 'android' || !Native?.isMonitoring) return false;
  try {
    return Boolean(await Native.isMonitoring());
  } catch {
    return false;
  }
}
