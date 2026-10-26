import { Platform } from 'react-native';

/**
 * Tiny async key-value store without pulling @react-native-async-storage
 * as a hard dependency during scaffolding. Uses in-memory + optional native
 * AsyncStorage if present.
 */
type Dict = Record<string, unknown>;

const memory = new Map<string, string>();

async function getNative(): Promise<{
  getItem: (k: string) => Promise<string | null>;
  setItem: (k: string, v: string) => Promise<void>;
} | null> {
  try {
    // Optional peer — ignore if not installed yet
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require('@react-native-async-storage/async-storage');
    return mod.default ?? mod;
  } catch {
    return null;
  }
}

const AsyncStorageShim = {
  async getJSON<T>(key: string): Promise<T | null> {
    const native = await getNative();
    const raw = native ? await native.getItem(key) : memory.get(key) ?? null;
    if (!raw) return null;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return null;
    }
  },

  async merge(key: string, partial: Dict): Promise<void> {
    const prev = (await this.getJSON<Dict>(key)) ?? {};
    const next = { ...prev, ...partial, _platform: Platform.OS };
    const raw = JSON.stringify(next);
    const native = await getNative();
    if (native) await native.setItem(key, raw);
    else memory.set(key, raw);
  },
};

export default AsyncStorageShim;
