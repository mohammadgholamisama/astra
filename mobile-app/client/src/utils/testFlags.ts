/** Multi-select test toggles — at least one must stay on. */
export type TestFlags = {
  ping: boolean;
  speed: boolean;
  packet: boolean;
};

export const DEFAULT_TEST_FLAGS: TestFlags = {
  ping: true,
  speed: false,
  packet: false,
};

export const TEST_FLAG_OPTIONS: readonly {
  id: keyof TestFlags;
  title: string;
}[] = [
  { id: 'packet', title: 'پکت' },
  { id: 'speed', title: 'سرعت' },
  { id: 'ping', title: 'پینگ' },
] as const;

export function normalizeTestFlags(raw: unknown): TestFlags {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_TEST_FLAGS };
  const o = raw as Record<string, unknown>;
  const flags: TestFlags = {
    ping: o.ping === true,
    speed: o.speed === true,
    packet: o.packet === true,
  };
  if (!flags.ping && !flags.speed && !flags.packet) {
    return { ...DEFAULT_TEST_FLAGS };
  }
  return flags;
}

/** Migrate legacy testMode / speedRankEnabled → TestFlags. */
export function testFlagsFromSettings(saved: {
  testFlags?: unknown;
  testMode?: unknown;
  speedRankEnabled?: boolean;
}): TestFlags {
  if (saved.testFlags != null) return normalizeTestFlags(saved.testFlags);
  const mode = saved.testMode;
  if (mode === 'ping_speed') return { ping: true, speed: true, packet: false };
  if (mode === 'speed') return { ping: false, speed: true, packet: false };
  if (mode === 'ping') return { ping: true, speed: false, packet: false };
  if (saved.speedRankEnabled === true) {
    return { ping: true, speed: true, packet: false };
  }
  return { ...DEFAULT_TEST_FLAGS };
}

export function toggleTestFlag(flags: TestFlags, id: keyof TestFlags): TestFlags {
  const next = { ...flags, [id]: !flags[id] };
  if (!next.ping && !next.speed && !next.packet) {
    // Keep at least one on — reverting the toggle
    return flags;
  }
  return next;
}

export function testFlagsHint(flags: TestFlags): string {
  const parts: string[] = [];
  if (flags.packet) parts.push('پکت');
  if (flags.speed) parts.push('سرعت');
  if (flags.ping) parts.push('پینگ');
  const selected = parts.length ? parts.join('+') : 'پینگ';
  return `${selected} · اجرا و رتبه‌بندی: پکت→سرعت→پینگ`;
}

export function testFlagsButtonHint(flags: TestFlags): string {
  const parts: string[] = [];
  if (flags.packet) parts.push('پکت');
  if (flags.speed) parts.push('سرعت');
  if (flags.ping) parts.push('پینگ');
  return parts.length ? parts.join(' · ') : 'پینگ';
}

/** Legacy TestMode snapshot for older readers / background. */
export function testFlagsToLegacyMode(flags: TestFlags): 'ping' | 'ping_speed' | 'speed' {
  if (flags.speed && !flags.ping && !flags.packet) return 'speed';
  if (flags.speed) return 'ping_speed';
  return 'ping';
}
