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
      <Text style={[styles.avg, { color }]}>{avgLabel}</Text>
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
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    backgroundColor: colors.panel,
    gap: 8,
  },
  cardActive: {
    borderColor: 'rgba(42,212,197,0.5)',
    backgroundColor: '#101a28',
  },
  rank: {
    width: 28,
    textAlign: 'center',
    color: colors.accent,
    fontSize: 15,
    fontFamily: fonts.extraBold,
  },
  body: { flex: 1, alignItems: 'flex-end' },
  title: {
    width: '100%',
    textAlign: 'right',
    color: colors.text,
    fontSize: 13,
    fontFamily: fonts.bold,
  },
  host: {
    marginTop: 2,
    width: '100%',
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.regular,
  },
  avg: {
    minWidth: 52,
    textAlign: 'left',
    fontSize: 15,
    fontFamily: fonts.extraBold,
  },
});
