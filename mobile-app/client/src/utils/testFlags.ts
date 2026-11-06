/** Multi-select test toggles — at least one must stay on. */
export type TestFlags = {
  ping: boolean;
  jitter: boolean;
  stability: boolean;
  packet: boolean;
  download: boolean;
  upload: boolean;
};

export const DEFAULT_TEST_FLAGS: TestFlags = {
  ping: true,
  jitter: false,
  stability: false,
  packet: false,
  download: false,
  upload: false,
};

export const TEST_FLAG_OPTIONS: readonly {
  id: keyof TestFlags;
  title: string;
  hint: string;
}[] = [
  { id: 'stability', title: 'پایداری', hint: 'نرخ موفقیت چند نمونه' },
  { id: 'packet', title: 'پکت', hint: 'درصد از دست رفتن' },
  { id: 'jitter', title: 'جیتر', hint: 'نوسان تأخیر' },
  { id: 'ping', title: 'پینگ', hint: 'تأخیر میانگین' },
  { id: 'download', title: 'دانلود', hint: 'سرعت دریافت' },
  { id: 'upload', title: 'آپلود', hint: 'سرعت ارسال' },
] as const;

function anyOn(f: TestFlags): boolean {
  return (
    f.ping ||
    f.jitter ||
    f.stability ||
    f.packet ||
    f.download ||
    f.upload
  );
}

export function normalizeTestFlags(raw: unknown): TestFlags {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_TEST_FLAGS };
  const o = raw as Record<string, unknown>;
  // Legacy: speed → both directions
  const legacySpeed = o.speed === true;
  const flags: TestFlags = {
    ping: o.ping === true,
    jitter: o.jitter === true,
    stability: o.stability === true,
    packet: o.packet === true,
    download: o.download === true || legacySpeed,
    upload: o.upload === true || legacySpeed,
  };
  if (!anyOn(flags)) return { ...DEFAULT_TEST_FLAGS };
  return flags;
}

/** Migrate legacy testMode / speedRankEnabled / speed flag → TestFlags. */
export function testFlagsFromSettings(saved: {
  testFlags?: unknown;
  testMode?: unknown;
  speedRankEnabled?: boolean;
}): TestFlags {
  if (saved.testFlags != null) return normalizeTestFlags(saved.testFlags);
  const mode = saved.testMode;
  if (mode === 'ping_speed') {
    return {
      ping: true,
      jitter: false,
      stability: false,
      packet: false,
      download: true,
      upload: true,
    };
  }
  if (mode === 'speed') {
    return {
      ping: false,
      jitter: false,
      stability: false,
      packet: false,
      download: true,
      upload: true,
    };
  }
  if (mode === 'ping') return { ...DEFAULT_TEST_FLAGS };
  if (saved.speedRankEnabled === true) {
    return {
      ping: true,
      jitter: false,
      stability: false,
      packet: false,
      download: true,
      upload: true,
    };
  }
  return { ...DEFAULT_TEST_FLAGS };
}

export function toggleTestFlag(flags: TestFlags, id: keyof TestFlags): TestFlags {
  const next = { ...flags, [id]: !flags[id] };
  if (!anyOn(next)) return flags;
  return next;
}

/** Need multi-sample Real Delay pass. */
export function needsLatencyPass(flags: TestFlags): boolean {
  return flags.ping || flags.jitter || flags.stability;
}

/** Multi samples when jitter/stability on; else single (+ native retry). */
export function latencySampleCount(flags: TestFlags): number {
  if (flags.jitter || flags.stability) return 3;
  return 1;
}

export function testFlagsHint(flags: TestFlags): string {
  const parts = TEST_FLAG_OPTIONS.filter((o) => flags[o.id]).map((o) => o.title);
  const selected = parts.length ? parts.join(' · ') : 'پینگ';
  return `${selected} · چیدمان: پایداری→پکت→جیتر→پینگ→دانلود→آپلود`;
}

export function testFlagsButtonHint(flags: TestFlags): string {
  const parts = TEST_FLAG_OPTIONS.filter((o) => flags[o.id]).map((o) => o.title);
  return parts.length ? parts.join(' · ') : 'پینگ';
}

/** Legacy TestMode snapshot for older readers / background. */
export function testFlagsToLegacyMode(
  flags: TestFlags,
): 'ping' | 'ping_speed' | 'speed' {
  const speed = flags.download || flags.upload;
  const latency = needsLatencyPass(flags) || flags.packet;
  if (speed && !latency) return 'speed';
  if (speed) return 'ping_speed';
  return 'ping';
}
