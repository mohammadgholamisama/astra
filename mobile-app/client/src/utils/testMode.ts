/** How a cycle measures and sorts configs. */
export type TestMode = 'ping' | 'ping_speed' | 'speed';

export const TEST_MODES: readonly TestMode[] = [
  'ping',
  'ping_speed',
  'speed',
] as const;

export const TEST_MODE_OPTIONS: readonly {
  id: TestMode;
  title: string;
  hint: string;
}[] = [
  {
    id: 'ping',
    title: 'پینگ',
    hint: 'فقط پینگ · ترتیب بر اساس کم‌ترین تأخیر',
  },
  {
    id: 'ping_speed',
    title: 'پینگ+سرعت',
    hint: 'اول پینگ، بعد سرعت روی وصل‌ها · ترتیب دانلود→آپلود',
  },
  {
    id: 'speed',
    title: 'سرعت',
    hint: 'سرعت روی همه از اول تا آخر · ترتیب دانلود→آپلود',
  },
] as const;

export function normalizeTestMode(raw: unknown): TestMode {
  if (raw === 'ping' || raw === 'ping_speed' || raw === 'speed') return raw;
  return 'ping';
}

/** Migrate legacy speedRankEnabled boolean → TestMode. */
export function testModeFromSettings(saved: {
  testMode?: unknown;
  speedRankEnabled?: boolean;
}): TestMode {
  if (
    saved.testMode === 'ping' ||
    saved.testMode === 'ping_speed' ||
    saved.testMode === 'speed'
  ) {
    return saved.testMode;
  }
  return saved.speedRankEnabled === true ? 'ping_speed' : 'ping';
}

export function testModeLabel(mode: TestMode): string {
  return TEST_MODE_OPTIONS.find((o) => o.id === mode)?.title ?? 'پینگ';
}

export function testModeHint(mode: TestMode): string {
  return TEST_MODE_OPTIONS.find((o) => o.id === mode)?.hint ?? '';
}

export function usesPing(mode: TestMode): boolean {
  return mode === 'ping' || mode === 'ping_speed';
}

export function usesSpeed(mode: TestMode): boolean {
  return mode === 'ping_speed' || mode === 'speed';
}
