/** Multi-select test toggles — at least one must stay on. */
export type TestFlags = {
  ping: boolean;
  jitter: boolean;
  stability: boolean;
  uptime: boolean;
  packet: boolean;
  download: boolean;
  upload: boolean;
};

export const DEFAULT_TEST_FLAGS: TestFlags = {
  ping: true,
  jitter: false,
  stability: false,
  uptime: false,
  packet: false,
  download: false,
  upload: false,
};

/** Chip order follows ranking priority (RTL: first = strongest). */
export const TEST_FLAG_OPTIONS: readonly {
  id: keyof TestFlags;
  title: string;
  hint: string;
}[] = [
  {
    id: 'stability',
    title: 'پایداری',
    hint: '۶ مقصد: gstatic · گوگل · کلادفلر · تلگرام · یوتیوب · اینستا',
  },
  {
    id: 'uptime',
    title: 'آپ‌تایم',
    hint: 'چند چرخهٔ پشت‌سرهم وصل بوده (جدید جریمه نمی‌شود)',
  },
  { id: 'jitter', title: 'جیتر', hint: 'نوسان تأخیر' },
  { id: 'ping', title: 'پینگ', hint: 'تأخیر میانگین' },
  { id: 'upload', title: 'آپلود', hint: 'سرعت ارسال' },
  { id: 'download', title: 'دانلود', hint: 'سرعت دریافت' },
  { id: 'packet', title: 'پکت', hint: 'درصد از دست رفتن' },
] as const;

/** Fixed ranking priority — arrows point left for RTL reading. */
export const RANK_PRIORITY_LABEL =
  'پایداری ← آپ‌تایم ← جیتر ← پینگ ← آپلود ← دانلود ← پکت';

function anyOn(f: TestFlags): boolean {
  return (
    f.ping ||
    f.jitter ||
    f.stability ||
    f.uptime ||
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
    uptime: o.uptime === true,
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
      uptime: false,
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
      uptime: false,
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
      uptime: false,
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

/** Need multi-sample Real Delay pass (uptime alone still needs a ping to score alive). */
export function needsLatencyPass(flags: TestFlags): boolean {
  return flags.ping || flags.jitter || flags.stability || flags.uptime;
}

/**
 * Sample count for latency pass.
 * Stability → 6 fixed destinations (gstatic/google/cf/tg/yt/ig).
 * Jitter only → 3 samples for MAD.
 * Else → 1 (+ native retry).
 */
export function latencySampleCount(flags: TestFlags): number {
  if (flags.stability) return 6;
  if (flags.jitter) return 3;
  return 1;
}

/** Wall budget for each Real Delay sample (ms). Same for every sample. */
export function latencyTimeoutMs(flags: TestFlags): number {
  // Multi-sample: 10s each (native still gets a full measure window).
  // Single ping: 12s to match LibXray default.
  if (flags.jitter || flags.stability) return 10_000;
  return 12_000;
}

export function testFlagsHint(flags: TestFlags): string {
  const parts = TEST_FLAG_OPTIONS.filter((o) => flags[o.id]).map((o) => o.title);
  const selected = parts.length ? parts.join(' · ') : 'پینگ';
  return `${selected} · اولویت: ${RANK_PRIORITY_LABEL}`;
}

export function rankPriorityHint(): string {
  return `اولویت ترتیب: ${RANK_PRIORITY_LABEL}`;
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
