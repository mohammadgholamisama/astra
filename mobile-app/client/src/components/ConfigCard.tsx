import React, { memo } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import type { RankedConfig } from '../types';
import { formatMs, toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

/** Fixed height so FlatList getItemLayout stays correct. */
const ROW_H = 112;

type Tone = 'mint' | 'amber' | 'accent' | 'rose' | 'mute' | 'violet';

type MetricCell = {
  key: string;
  label: string;
  value: string;
  tone: Tone;
};

function pingColor(ms: number, testing: boolean, fail: boolean, wait: boolean): string {
  if (testing) return colors.accent;
  if (wait) return colors.textMute;
  if (fail || ms < 0) return colors.rose;
  if (ms < 180) return colors.mint;
  if (ms < 350) return colors.accent;
  if (ms < 600) return colors.amber;
  return colors.rose;
}

function formatKbps(kbps?: number): string {
  if (kbps == null) return '—';
  if (!(kbps > 0)) return '—';
  if (kbps >= 1000) return `${toFaDigits((kbps / 1000).toFixed(1))}M`;
  return `${toFaDigits(Math.round(kbps))}k`;
}

function formatLoss(pct?: number): string {
  if (pct == null) return '—';
  if (pct < 0) return '—';
  // Keep exact measured percent (e.g. 97) — no coarse rounding
  const n = Math.round(pct * 10) / 10;
  const text = Number.isInteger(n) ? String(n) : n.toFixed(1);
  return `${toFaDigits(text)}٪`;
}

function formatJitter(ms?: number): string {
  if (ms == null) return '—';
  if (ms < 0) return '—';
  return toFaDigits(Math.round(ms));
}

function formatStab(pct?: number): string {
  if (pct == null) return '—';
  if (pct < 0) return '—';
  return `${toFaDigits(pct % 1 === 0 ? String(pct) : pct.toFixed(0))}٪`;
}

function stabTone(pct?: number): Tone {
  if (pct == null || pct < 0) return 'mute';
  if (pct >= 90) return 'mint';
  if (pct >= 70) return 'accent';
  if (pct >= 40) return 'amber';
  return 'rose';
}

function lossTone(pct?: number): Tone {
  if (pct == null || pct < 0) return 'mute';
  if (pct <= 2) return 'mint';
  if (pct <= 8) return 'accent';
  if (pct <= 20) return 'amber';
  return 'rose';
}

function jitterTone(ms?: number): Tone {
  if (ms == null || ms < 0) return 'mute';
  if (ms <= 20) return 'mint';
  if (ms <= 50) return 'accent';
  if (ms <= 100) return 'amber';
  return 'rose';
}

function speedTone(kbps?: number): Tone {
  if (kbps == null || !(kbps > 0)) return 'mute';
  if (kbps >= 8000) return 'mint';
  if (kbps >= 2500) return 'accent';
  if (kbps >= 800) return 'amber';
  return 'rose';
}

function toneColor(tone: Tone): string {
  switch (tone) {
    case 'mint':
      return colors.mint;
    case 'amber':
      return colors.amber;
    case 'accent':
      return colors.accent;
    case 'rose':
      return colors.rose;
    case 'violet':
      return colors.violet;
    default:
      return colors.textMute;
  }
}

function toneBg(tone: Tone): string {
  switch (tone) {
    case 'mint':
      return 'rgba(74,222,128,0.12)';
    case 'amber':
      return 'rgba(251,191,36,0.12)';
    case 'accent':
      return 'rgba(42,212,197,0.12)';
    case 'rose':
      return 'rgba(251,113,133,0.12)';
    case 'violet':
      return 'rgba(167,139,250,0.12)';
    default:
      return 'rgba(255,255,255,0.05)';
  }
}

function buildMetrics(item: RankedConfig): MetricCell[] {
  const cells: MetricCell[] = [];

  if (item.stability_pct != null) {
    cells.push({
      key: 'stab',
      label: 'پایدار',
      value: formatStab(item.stability_pct),
      tone: stabTone(item.stability_pct),
    });
  }
  if (item.uptime_pct != null || (item.uptime_samples != null && item.uptime_samples > 0)) {
    const samples = item.uptime_samples ?? 0;
    const ready = samples >= 2;
    cells.push({
      key: 'up',
      label: 'آپ‌تایم',
      value: ready
        ? formatStab(item.uptime_pct)
        : samples > 0
          ? 'جدید'
          : '—',
      tone: ready ? stabTone(item.uptime_pct) : 'violet',
    });
  }
  if (item.jitter_ms != null) {
    cells.push({
      key: 'jit',
      label: 'جیتر',
      value: formatJitter(item.jitter_ms),
      tone: jitterTone(item.jitter_ms),
    });
  }
  if (item.packet_loss_pct != null) {
    cells.push({
      key: 'loss',
      label: 'پکت',
      value: formatLoss(item.packet_loss_pct),
      tone: lossTone(item.packet_loss_pct),
    });
  }
  if (item.download_kbps != null) {
    cells.push({
      key: 'dl',
      label: 'دانلود',
      value: formatKbps(item.download_kbps),
      tone: speedTone(item.download_kbps),
    });
  }
  if (item.upload_kbps != null) {
    cells.push({
      key: 'ul',
      label: 'آپلود',
      value: formatKbps(item.upload_kbps),
      tone: speedTone(item.upload_kbps),
    });
  }

  return cells;
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

  const metrics = buildMetrics(item);
  const hasMetrics = metrics.length > 0;

  return (
    <Pressable
      onPress={onDetails}
      style={[styles.card, highlight && styles.cardActive]}
    >
      <Text style={styles.rank}>{toFaDigits(item.rank)}</Text>
      <View style={styles.body}>
        <View style={styles.topRow}>
          <View style={styles.titleBlock}>
            <Text style={styles.title} numberOfLines={1}>
              {item.remark || item.host || 'بدون نام'}
            </Text>
            <Text style={styles.host} numberOfLines={1}>
              {item.host}:{item.port}
              {item.protocol ? ` · ${item.protocol}` : ''}
            </Text>
          </View>
          <View style={styles.pingBox}>
            <Text style={[styles.avg, { color }]}>{avgLabel}</Text>
            <Text style={styles.avgHint}>پینگ</Text>
          </View>
        </View>

        {hasMetrics ? (
          <View style={styles.metricRow}>
            {metrics.map((m) => (
              <View
                key={m.key}
                style={[styles.metricCell, { backgroundColor: toneBg(m.tone) }]}
              >
                <Text style={styles.metricLabel} numberOfLines={1}>
                  {m.label}
                </Text>
                <Text
                  style={[styles.metricValue, { color: toneColor(m.tone) }]}
                  numberOfLines={1}
                >
                  {m.value}
                </Text>
              </View>
            ))}
          </View>
        ) : null}
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
    a.item.uptime_pct === b.item.uptime_pct &&
    a.item.uptime_samples === b.item.uptime_samples &&
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
    alignItems: 'stretch',
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
    width: 26,
    paddingTop: 4,
    textAlign: 'center',
    color: colors.textMute,
    fontFamily: fonts.bold,
    fontSize: 13,
  },
  body: { flex: 1, paddingHorizontal: 4 },
  topRow: {
    flexDirection: 'row-reverse',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  },
  titleBlock: { flex: 1, minWidth: 0 },
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
    fontSize: 10,
    fontFamily: fonts.regular,
  },
  pingBox: {
    alignItems: 'flex-end',
    minWidth: 52,
    paddingStart: 2,
  },
  avg: {
    fontFamily: fonts.extraBold,
    fontSize: 15,
    fontVariant: ['tabular-nums'],
  },
  avgHint: {
    marginTop: 1,
    color: colors.textMute,
    fontSize: 9,
    fontFamily: fonts.medium,
  },
  metricRow: {
    marginTop: 8,
    flexDirection: 'row-reverse',
    flexWrap: 'nowrap',
    gap: 4,
  },
  metricCell: {
    flexGrow: 1,
    flexBasis: 0,
    minWidth: 0,
    paddingHorizontal: 4,
    paddingVertical: 5,
    borderRadius: 8,
    alignItems: 'center',
  },
  metricLabel: {
    color: colors.textMute,
    fontSize: 9,
    fontFamily: fonts.medium,
    marginBottom: 2,
  },
  metricValue: {
    fontSize: 11,
    fontFamily: fonts.bold,
    fontVariant: ['tabular-nums'],
  },
});
