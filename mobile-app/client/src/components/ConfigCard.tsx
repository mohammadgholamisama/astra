import React, { memo } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import type { RankedConfig } from '../types';
import { formatMs, toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

const ROW_H = 64;

function pingColor(ms: number, testing: boolean, fail: boolean, wait: boolean): string {
  if (testing) return colors.accent;
  if (wait) return colors.textMute;
  if (fail || ms < 0) return colors.rose;
  if (ms < 180) return colors.mint;
  if (ms < 350) return colors.accent;
  if (ms < 600) return colors.amber;
  return colors.rose;
}

function formatKbps(kbps?: number): string | null {
  if (kbps == null || !(kbps > 0)) return null;
  if (kbps >= 1000) return `${toFaDigits((kbps / 1000).toFixed(1))}M`;
  return `${toFaDigits(Math.round(kbps))}k`;
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
  const fail = measured && avg < 0;
  const wait = !testing && !measured;
  const color = pingColor(avg, testing, fail, wait);
  const avgLabel = testing ? '…' : fail ? 'قطع' : wait ? '—' : formatMs(avg);
  const dl = formatKbps(item.download_kbps);
  const ul = formatKbps(item.upload_kbps);
  const speedLabel =
    dl || ul ? `${dl ? `${dl}↓` : ''}${dl && ul ? ' ' : ''}${ul ? `${ul}↑` : ''}` : null;

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
      </View>
      <View style={styles.metrics}>
        {speedLabel ? (
          <Text style={styles.speed} numberOfLines={1}>
            {speedLabel}
          </Text>
        ) : null}
        <Text style={[styles.avg, { color }]}>{avgLabel}</Text>
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
    a.item.measured === b.item.measured &&
    a.item.remark === b.item.remark
  );
}

export const ConfigCard = memo(ConfigCardInner, propsEqual);
export const CONFIG_ROW_HEIGHT = ROW_H;

const styles = StyleSheet.create({
  card: {
    height: ROW_H,
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 6,
    paddingHorizontal: 10,
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
  metrics: { alignItems: 'flex-end', minWidth: 72 },
  speed: {
    color: colors.mint,
    fontSize: 10,
    fontFamily: fonts.medium,
    marginBottom: 2,
  },
  avg: {
    fontFamily: fonts.extraBold,
    fontSize: 14,
    fontVariant: ['tabular-nums'],
  },
});
