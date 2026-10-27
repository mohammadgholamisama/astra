import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Gauge, X, Zap } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';
import { formatCountdown, toFaDigits } from '../utils/format';
import { CYCLE_OPTIONS } from '../utils/cycleInterval';

export function TestActionBar({
  testing,
  disabled,
  remainingMs,
  cycleIntervalMs,
  progressDone = 0,
  progressTotal = 0,
  progressStatus = '',
  onTest,
  onCancel,
  onCycleInterval,
}: {
  testing: boolean;
  disabled: boolean;
  remainingMs: number;
  cycleIntervalMs: number;
  progressDone?: number;
  progressTotal?: number;
  progressStatus?: string;
  onTest: () => void;
  onCancel: () => void;
  onCycleInterval: (ms: number) => void;
}) {
  const pct =
    progressTotal > 0
      ? Math.min(100, Math.round((progressDone / progressTotal) * 100))
      : 0;

  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        {testing ? (
          <Pressable
            onPressIn={onCancel}
            onPress={onCancel}
            hitSlop={16}
            accessibilityRole="button"
            accessibilityLabel="قطع تست"
            style={({ pressed }) => [
              styles.btn,
              styles.btnCancel,
              pressed && styles.btnPressed,
            ]}
          >
            <View style={styles.btnCopy}>
              <Text style={styles.btnTitleCancel}>قطع تست</Text>
              <Text style={styles.btnHintCancel}>الان متوقف شود</Text>
            </View>
            <View style={[styles.btnIcon, styles.btnIconCancel]}>
              <X size={18} color={colors.bg} strokeWidth={2.6} />
            </View>
          </Pressable>
        ) : (
          <Pressable
            disabled={disabled}
            onPress={onTest}
            style={({ pressed }) => [
              styles.btn,
              disabled && styles.btnDisabled,
              pressed && !disabled && styles.btnPressed,
            ]}
          >
            <View style={styles.btnCopy}>
              <Text style={styles.btnTitle}>تست الان</Text>
              <Text style={styles.btnHint}>پینگ واقعی · سریع</Text>
            </View>
            <View style={styles.btnIcon}>
              <Zap
                size={18}
                color={colors.bg}
                strokeWidth={2.4}
                fill={colors.bg}
              />
            </View>
          </Pressable>
        )}

        <View style={styles.timer}>
          <View style={styles.timerTop}>
            <Text style={styles.timerLabel}>چرخه بعدی</Text>
            <Gauge size={13} color={colors.accent} strokeWidth={2.2} />
          </View>
          <Text style={styles.timerValue}>
            {testing ? '—' : formatCountdown(remainingMs)}
          </Text>
        </View>
      </View>

      {testing ? (
        <View style={styles.progressCard}>
          <View style={styles.progressTop}>
            <Text style={styles.progressStatus} numberOfLines={1}>
              {progressStatus || 'پینگ…'}
            </Text>
            <Text style={styles.progressPct}>{toFaDigits(pct)}٪</Text>
          </View>
          <View style={styles.progressTrack}>
            <View style={[styles.progressFill, { width: `${pct}%` }]} />
          </View>
          <Text style={styles.progressMeta}>
            {toFaDigits(progressDone)} از {toFaDigits(progressTotal)}
          </Text>
        </View>
      ) : null}

      <View style={styles.intervalBlock}>
        <Text style={styles.intervalTitle}>بازهٔ چرخه (دقیقه)</Text>
        <View style={styles.chips}>
          {CYCLE_OPTIONS.map((opt) => {
            const active = opt.ms === cycleIntervalMs;
            return (
              <Pressable
                key={opt.ms}
                disabled={testing}
                onPress={() => onCycleInterval(opt.ms)}
                style={({ pressed }) => [
                  styles.chip,
                  active && styles.chipActive,
                  testing && styles.chipDisabled,
                  pressed && !testing && styles.chipPressed,
                ]}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                accessibilityLabel={`چرخه ${opt.minutes} دقیقه`}
              >
                <Text
                  style={[styles.chipText, active && styles.chipTextActive]}
                >
                  {toFaDigits(opt.label)}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: 'rgba(42,212,197,0.22)',
    backgroundColor: colors.bgElevated,
    padding: 12,
    gap: 12,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
  },
  btn: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    borderRadius: radii.lg,
    backgroundColor: colors.accent,
    paddingVertical: 14,
    paddingHorizontal: 12,
    minHeight: 64,
  },
  btnCancel: {
    backgroundColor: '#E85D4C',
  },
  btnPressed: { opacity: 0.88 },
  btnDisabled: { backgroundColor: 'rgba(255,255,255,0.1)' },
  btnIcon: {
    width: 34,
    height: 34,
    borderRadius: 12,
    backgroundColor: 'rgba(5,8,15,0.12)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  btnIconCancel: {
    backgroundColor: 'rgba(5,8,15,0.18)',
  },
  btnCopy: { alignItems: 'flex-end' },
  btnTitle: { color: colors.bg, fontFamily: fonts.extraBold, fontSize: 16 },
  btnHint: {
    color: 'rgba(5,8,15,0.55)',
    fontSize: 10,
    marginTop: 2,
    fontFamily: fonts.medium,
  },
  btnTitleCancel: {
    color: colors.bg,
    fontFamily: fonts.extraBold,
    fontSize: 16,
  },
  btnHintCancel: {
    color: 'rgba(5,8,15,0.65)',
    fontSize: 10,
    marginTop: 2,
    fontFamily: fonts.medium,
  },
  timer: {
    minWidth: 96,
    borderRadius: radii.lg,
    backgroundColor: colors.panel,
    borderWidth: 1,
    borderColor: colors.line,
    paddingVertical: 10,
    paddingHorizontal: 12,
    alignItems: 'flex-start',
  },
  timerTop: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    alignSelf: 'stretch',
    justifyContent: 'flex-start',
  },
  timerLabel: {
    color: colors.textMute,
    fontSize: 10,
    fontFamily: fonts.medium,
  },
  timerValue: {
    marginTop: 4,
    textAlign: 'right',
    alignSelf: 'stretch',
    color: colors.accent,
    fontSize: 26,
    fontFamily: fonts.extraBold,
    fontVariant: ['tabular-nums'],
  },
  progressCard: {
    borderRadius: radii.lg,
    backgroundColor: colors.panel,
    borderWidth: 1,
    borderColor: colors.line,
    paddingVertical: 10,
    paddingHorizontal: 12,
    gap: 8,
  },
  progressTop: {
    flexDirection: 'row-reverse',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  },
  progressStatus: {
    flex: 1,
    textAlign: 'right',
    color: colors.textSoft,
    fontSize: 12,
    fontFamily: fonts.medium,
  },
  progressPct: { color: colors.mint, fontFamily: fonts.extraBold, fontSize: 14 },
  progressTrack: {
    height: 8,
    borderRadius: 99,
    backgroundColor: 'rgba(255,255,255,0.08)',
    overflow: 'hidden',
  },
  progressFill: { height: '100%', borderRadius: 99, backgroundColor: colors.mint },
  progressMeta: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.medium,
    fontVariant: ['tabular-nums'],
  },
  intervalBlock: {
    gap: 8,
  },
  intervalTitle: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.medium,
  },
  chips: {
    flexDirection: 'row-reverse',
    flexWrap: 'wrap',
    gap: 8,
    justifyContent: 'flex-start',
  },
  chip: {
    minWidth: 44,
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    alignItems: 'center',
  },
  chipActive: {
    borderColor: 'rgba(42,212,197,0.55)',
    backgroundColor: colors.accentDim,
  },
  chipDisabled: { opacity: 0.45 },
  chipPressed: { opacity: 0.85 },
  chipText: {
    color: colors.textSoft,
    fontSize: 14,
    fontFamily: fonts.bold,
    fontVariant: ['tabular-nums'],
  },
  chipTextActive: {
    color: colors.accent,
    fontFamily: fonts.extraBold,
  },
});
