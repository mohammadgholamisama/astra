import React, { memo } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import type { RankedConfig } from '../types';
import { formatMs, toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

const ROW_H = 86;

function pingColor(ms: number, testing: boolean, fail: boolean, wait: boolean): string {
  if (testing) return colors.accent;
  if (wait) return colors.textMute;
  if (fail || ms < 0) return colors.rose;
  if (ms < 180) return colors.mint;
  if (ms < 350) return colors.accent;
  if (ms < 600) return colors.amber;
  return colors.rose;
}

/** kbps → label; null = not tested yet; ≤0 after test → — */
function formatKbpsPart(kbps?: number): string | null {
  if (kbps == null) return null;
  if (!(kbps > 0)) return '—';
  if (kbps >= 1000) return `${toFaDigits((kbps / 1000).toFixed(1))}M`;
  return `${toFaDigits(Math.round(kbps))}k`;
}

function formatLossPart(pct?: number): string | null {
  if (pct == null) return null;
  if (pct < 0) return '—';
  return `${toFaDigits(pct % 1 === 0 ? String(pct) : pct.toFixed(1))}٪`;
}

function formatJitter(ms?: number): string | null {
  if (ms == null) return null;
  if (ms < 0) return '—';
  return `${toFaDigits(Math.round(ms))}ms`;
}

function formatStab(pct?: number): string | null {
  if (pct == null) return null;
  if (pct < 0) return '—';
  return `${toFaDigits(pct % 1 === 0 ? String(pct) : pct.toFixed(0))}٪`;
}

function ConfigCardInner({
  item,
  highlight,
  onDetails,
}: {
  item: RankedConfig;
  highlight?: boolean;
  onDetails?: () => void;
}) {
  const avg = item.avg_ping;
  const testing = avg === -2 || !!highlight;
  const measured = !!item.measured;
  const pingSkipped = measured && avg === 0;
  const fail = measured && avg < 0;
  const wait = !testing && !measured;
  const color = pingSkipped
    ? colors.textMute
    : pingColor(avg, testing, fail, wait);
  const avgLabel = testing
    ? '…'
    : pingSkipped
      ? '—'
      : fail
        ? 'قطع'
        : wait
          ? '—'
          : formatMs(avg);

  const chips: { key: string; text: string; tone: 'amber' | 'mint' | 'accent' | 'mute' }[] =
    [];

  if (item.stability_pct != null) {
    chips.push({
      key: 'stab',
      text: `پاید ${formatStab(item.stability_pct)}`,
      tone: 'accent',
    });
  }
  if (item.packet_loss_pct != null) {
    chips.push({
      key: 'loss',
      text: `پکت ${formatLossPart(item.packet_loss_pct)}`,
      tone: 'amber',
    });
  }
  if (item.jitter_ms != null) {
    chips.push({
      key: 'jit',
      text: `جیتر ${formatJitter(item.jitter_ms)}`,
      tone: 'mute',
    });
  }
  if (item.download_kbps != null || item.upload_kbps != null) {
    const dl = formatKbpsPart(item.download_kbps) ?? '—';
    const ul = formatKbpsPart(item.upload_kbps) ?? '—';
    const onlyDl = item.download_kbps != null && item.upload_kbps == null;
    const onlyUl = item.upload_kbps != null && item.download_kbps == null;
    chips.push({
      key: 'spd',
      text: onlyDl
        ? `دان ${dl}`
        : onlyUl
          ? `آپ ${ul}`
          : `دان ${dl} · آپ ${ul}`,
      tone: 'mint',
    });
  }

  return (
    <Pressable
      onPress={onDetails}
      style={[styles.card, highlight && styles.cardActive]}
    >
      <Text style={styles.rank}>{toFaDigits(item.rank)}</Text>
      <View style={styles.body}>
        <Text style={styles.title} numberOfLines={1}>
          {item.remark || item.host || 'بدون نام'}
        </Text>
        <Text style={styles.host} numberOfLines={1}>
          {item.host}:{item.port}
          {item.protocol ? ` · ${item.protocol}` : ''}
        </Text>
        {chips.length > 0 ? (
          <View style={styles.chipRow}>
            {chips.slice(0, 4).map((c) => (
              <Text
                key={c.key}
                style={[
                  styles.chip,
                  c.tone === 'amber' && styles.chipAmber,
                  c.tone === 'mint' && styles.chipMint,
                  c.tone === 'accent' && styles.chipAccent,
                ]}
                numberOfLines={1}
              >
                {c.text}
              </Text>
            ))}
          </View>
        ) : null}
      </View>
      <View style={styles.metrics}>
        <Text style={[styles.avg, { color }]}>{avgLabel}</Text>
        <Text style={styles.avgHint}>پینگ</Text>
      </View>
    </Pressable>
  );
}

function propsEqual(
  a: { item: RankedConfig; highlight?: boolean },
  b: { item: RankedConfig; highlight?: boolean },
) {
  return (
    a.highlight === b.highlight &&
    a.item.config_hash === b.item.config_hash &&
    a.item.rank === b.item.rank &&
    a.item.avg_ping === b.item.avg_ping &&
    a.item.download_kbps === b.item.download_kbps &&
    a.item.upload_kbps === b.item.upload_kbps &&
    a.item.packet_loss_pct === b.item.packet_loss_pct &&
    a.item.jitter_ms === b.item.jitter_ms &&
    a.item.stability_pct === b.item.stability_pct &&
    a.item.measured === b.item.measured &&
    a.item.remark === b.item.remark
  );
}

export const ConfigCard = memo(ConfigCardInner, propsEqual);
export const CONFIG_ROW_HEIGHT = ROW_H;

const styles = StyleSheet.create({
  card: {
    minHeight: ROW_H,
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 6,
    paddingHorizontal: 10,
    paddingVertical: 8,
    borderRadius: radii.lg,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.line,
  },
  cardActive: {
    borderColor: 'rgba(42,212,197,0.45)',
  },
  rank: {
    width: 28,
    textAlign: 'center',
    color: colors.textMute,
    fontFamily: fonts.bold,
    fontSize: 13,
  },
  body: { flex: 1, paddingHorizontal: 6 },
  title: {
    textAlign: 'right',
    color: colors.text,
    fontFamily: fonts.bold,
    fontSize: 13,
  },
  host: {
    marginTop: 2,
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.regular,
  },
  chipRow: {
    marginTop: 5,
    flexDirection: 'row-reverse',
    flexWrap: 'wrap',
    gap: 4,
    justifyContent: 'flex-start',
  },
  chip: {
    overflow: 'hidden',
    maxWidth: '100%',
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 6,
    backgroundColor: 'rgba(255,255,255,0.06)',
    color: colors.textMute,
    fontSize: 10,
    fontFamily: fonts.medium,
  },
  chipAmber: { color: colors.amber, backgroundColor: 'rgba(245,158,11,0.12)' },
  chipMint: { color: colors.mint, backgroundColor: 'rgba(52,211,153,0.12)' },
  chipAccent: {
    color: colors.accent,
    backgroundColor: 'rgba(42,212,197,0.12)',
  },
  metrics: { alignItems: 'flex-end', minWidth: 56, paddingStart: 4 },
  avg: {
    fontFamily: fonts.extraBold,
    fontSize: 14,
    fontVariant: ['tabular-nums'],
  },
  avgHint: {
    marginTop: 1,
    color: colors.textMute,
    fontSize: 9,
    fontFamily: fonts.medium,
  },
});
