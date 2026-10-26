import { NativeModules, Share, ToastAndroid, Platform } from 'react-native';

/** Copy text; falls back to share sheet if clipboard module missing. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require('@react-native-clipboard/clipboard');
    const Clipboard = mod?.default ?? mod;
    if (Clipboard?.setString) {
      Clipboard.setString(text);
      if (Platform.OS === 'android') {
        ToastAndroid.show('کپی شد', ToastAndroid.SHORT);
      }
      return true;
    }
  } catch {
    /* fall through */
  }

  // Legacy / missing native module
  const legacy = (NativeModules as { Clipboard?: { setString?: (s: string) => void } })
    .Clipboard;
  if (legacy?.setString) {
    legacy.setString(text);
    if (Platform.OS === 'android') {
      ToastAndroid.show('کپی شد', ToastAndroid.SHORT);
    }
    return true;
  }

  try {
    await Share.share({ message: text });
    return true;
  } catch {
    return false;
  }
}
