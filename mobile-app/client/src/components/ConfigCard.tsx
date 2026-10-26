import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import {
  Info,
  Signal,
  SignalHigh,
  SignalLow,
  SignalMedium,
  SignalZero,
} from 'lucide-react-native';
import type { RankedConfig } from '../types';
import { formatMs, toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

function pingColor(ms: number, mode: 'wait' | 'fail' | 'live' = 'live'): string {
  if (ms === -2) return colors.accent;
  if (mode === 'wait') return colors.textMute;
  if (ms < 0 || mode === 'fail') return colors.rose;
  if (ms < 180) return colors.mint;
  if (ms < 350) return colors.accent;
  if (ms < 600) return colors.amber;
  return colors.rose;
}

function SignalGlyph({ ms, mode }: { ms: number; mode: 'wait' | 'fail' | 'live' }) {
  const c = pingColor(ms, mode);
  if (mode === 'wait' || ms === -2) return <Signal size={14} color={c} strokeWidth={2.2} />;
  if (ms < 0 || mode === 'fail') return <SignalZero size={14} color={c} strokeWidth={2.2} />;
  if (ms < 180) return <SignalHigh size={14} color={c} strokeWidth={2.2} />;
  if (ms < 350) return <SignalMedium size={14} color={c} strokeWidth={2.2} />;
  if (ms < 600) return <SignalLow size={14} color={c} strokeWidth={2.2} />;
  return <SignalZero size={14} color={c} strokeWidth={2.2} />;
}

export function ConfigCard({
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
  const measured = !!item.measured || (item.operator_pings?.length ?? 0) > 0;
  const doneFail = measured && avg < 0;
  const waiting = !testing && !measured;
  const mode = testing ? 'live' : doneFail ? 'fail' : waiting ? 'wait' : 'live';
  const avgLabel = testing ? '…' : doneFail ? 'قطع' : waiting ? '—' : formatMs(avg);
  const quality = testing
    ? 'در حال تست'
    : doneFail
      ? 'قطع'
      : waiting
        ? 'منتظر'
        : avg < 180
          ? 'عالی'
          : avg < 350
            ? 'خوب'
            : avg < 600
              ? 'متوسط'
              : 'ضعیف';

  return (
    <View style={[styles.card, highlight && styles.cardActive]}>
      <View style={[styles.rankCol, highlight && styles.rankColActive]}>
        <Text style={styles.rankHash}>رتبه</Text>
        <Text style={styles.rankNum}>{toFaDigits(item.rank)}</Text>
      </View>

      <View style={styles.body}>
        <View style={styles.titleRow}>
          <Pressable
            onPress={onDetails}
            hitSlop={8}
            accessibilityLabel="جزئیات کانفیگ"
            style={styles.infoBtn}
          >
            <Info size={16} color={colors.accent} strokeWidth={2.3} />
          </Pressable>

          <View style={styles.titleCol}>
            <Text style={styles.title} numberOfLines={1}>
              {item.remark || item.host || 'بدون نام'}
            </Text>
            <Text style={styles.host} numberOfLines={1}>
              {item.host}:{item.port}
            </Text>
          </View>

          <View style={styles.avgWrap}>
            {testing ? (
              <ActivityIndicator color={colors.accent} size="small" />
            ) : (
              <Text style={[styles.avg, { color: pingColor(avg, mode) }]}>{avgLabel}</Text>
            )}
            <View style={styles.qualityRow}>
              <Text style={[styles.quality, { color: pingColor(avg, mode) }]}>{quality}</Text>
              <SignalGlyph ms={avg} mode={mode} />
            </View>
          </View>
        </View>

        <View style={styles.metaRow}>
          {!!item.protocol && (
            <View style={[styles.chip, { backgroundColor: 'rgba(167,139,250,0.14)' }]}>
              <Text style={[styles.chipText, { color: colors.violet }]}>
                {item.protocol.toUpperCase()}
              </Text>
            </View>
          )}
          {item.security === 'reality' && (
            <View style={[styles.chip, { backgroundColor: 'rgba(74,222,128,0.12)' }]}>
              <Text style={[styles.chipText, { color: colors.mint }]}>Reality</Text>
            </View>
          )}
          {!!item.transport && item.transport !== 'tcp' && (
            <View style={[styles.chip, { backgroundColor: colors.accentDim }]}>
              <Text style={[styles.chipText, { color: colors.accent }]}>
                {item.transport.toUpperCase()}
              </Text>
            </View>
          )}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    flexDirection: 'row',
    marginBottom: 10,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.panel,
    overflow: 'hidden',
  },
  cardActive: {
    borderColor: 'rgba(42,212,197,0.45)',
    backgroundColor: '#122033',
  },
  rankCol: {
    width: 54,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.panelSoft,
    borderRightWidth: StyleSheet.hairlineWidth,
    borderRightColor: colors.lineStrong,
    paddingVertical: 12,
  },
  rankColActive: { backgroundColor: 'rgba(42,212,197,0.12)' },
  rankHash: { color: colors.textMute, fontSize: 9, fontFamily: fonts.medium },
  rankNum: { color: colors.accent, fontSize: 20, fontFamily: fonts.extraBold, marginTop: 2 },
  body: { flex: 1, paddingHorizontal: 13, paddingVertical: 12 },
  titleRow: {
    direction: 'ltr',
    flexDirection: 'row-reverse',
    alignItems: 'flex-start',
    gap: 8,
    marginBottom: 8,
  },
  infoBtn: {
    width: 32,
    height: 32,
    borderRadius: 10,
    backgroundColor: colors.accentDim,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 2,
  },
  titleCol: { flex: 1, alignItems: 'flex-end' },
  title: {
    alignSelf: 'stretch',
    textAlign: 'right',
    color: colors.text,
    fontSize: 14,
    fontFamily: fonts.bold,
  },
  host: {
    marginTop: 3,
    alignSelf: 'stretch',
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.regular,
  },
  avgWrap: { alignItems: 'flex-end', minWidth: 68 },
  avg: { fontSize: 19, fontFamily: fonts.extraBold },
  qualityRow: { flexDirection: 'row', alignItems: 'center', gap: 4, marginTop: 2 },
  quality: { fontSize: 10, fontFamily: fonts.bold },
  metaRow: {
    direction: 'ltr',
    flexDirection: 'row-reverse',
    flexWrap: 'wrap',
    gap: 6,
  },
  chip: { borderRadius: 8, paddingHorizontal: 8, paddingVertical: 3 },
  chipText: { fontSize: 10, fontFamily: fonts.bold },
});
