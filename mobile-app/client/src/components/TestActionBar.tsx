import React, { useRef } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Gauge, X, Zap } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';
import { formatCountdown, toFaDigits } from '../utils/format';
import {
  CYCLE_HOUR_OPTIONS,
  CYCLE_MINUTE_OPTIONS,
  cycleIntervalLabel,
  cycleOptionOf,
} from '../utils/cycleInterval';
import {
  TEST_FLAG_OPTIONS,
  testFlagsButtonHint,
  testFlagsHint,
  type TestFlags,
} from '../utils/testFlags';
import { useAppStore } from '../store/appStore';

/**
 * Subscribes to store itself so parent FlatList header does not remount
 * on every progress tick (that used to reset button locks / eat taps).
 */
export function TestActionBar({
  disabled,
  onTest,
  onCancel,
  onCycleInterval,
}: {
  disabled: boolean;
  onTest: () => void;
  onCancel: () => void;
  onCycleInterval: (ms: number) => void;
}) {
  const testing = useAppStore((s) => s.testing);
  const remainingMs = useAppStore((s) => s.remainingMs);
  const cycleIntervalMs = useAppStore((s) => s.cycleIntervalMs);
  const backgroundEnabled = useAppStore((s) => s.backgroundEnabled);
  const testFlags = useAppStore((s) => s.testFlags);
  const toggleTestFlag = useAppStore((s) => s.toggleTestFlag);
  const progressDone = useAppStore((s) => s.progressDone);
  const progressTotal = useAppStore((s) => s.progressTotal);
  const progressStatus = useAppStore((s) => s.status);

  const pct =
    progressTotal > 0
      ? Math.min(100, Math.round((progressDone / progressTotal) * 100))
      : 0;

  const cancelLock = useRef(false);
  const testLock = useRef(false);

  const handleCancel = () => {
    if (cancelLock.current) return;
    cancelLock.current = true;
    onCancel();
    setTimeout(() => {
      cancelLock.current = false;
    }, 900);
  };

  const handleTest = () => {
    if (testLock.current || testing || disabled) return;
    testLock.current = true;
    onTest();
    setTimeout(() => {
      testLock.current = false;
    }, 900);
  };

  const modeHint = testFlagsHint(testFlags);
  const testBtnHint = testFlagsButtonHint(testFlags);

  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        {/* Both stay mounted — swapping mid-press caused multi-tap bugs */}
        <View style={styles.btnSlot}>
          <Pressable
            onPress={handleCancel}
            hitSlop={12}
            accessibilityRole="button"
            accessibilityLabel="قطع تست"
            accessibilityState={{ disabled: !testing }}
            pointerEvents={testing ? 'auto' : 'none'}
            style={({ pressed }) => [
              styles.btn,
              styles.btnCancel,
              !testing && styles.btnGone,
              pressed && testing && styles.btnPressed,
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

          <Pressable
            onPress={handleTest}
            disabled={disabled || testing}
            hitSlop={12}
            accessibilityRole="button"
            accessibilityLabel="تست الان"
            accessibilityState={{ disabled: disabled || testing }}
            pointerEvents={testing ? 'none' : 'auto'}
            style={({ pressed }) => [
              styles.btn,
              styles.btnTest,
              testing && styles.btnGone,
              (disabled || testing) && styles.btnDisabled,
              pressed && !disabled && !testing && styles.btnPressed,
            ]}
          >
            <View style={styles.btnCopy}>
              <Text style={styles.btnTitle}>تست الان</Text>
              <Text style={styles.btnHint}>{testBtnHint}</Text>
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
        </View>

        <View style={styles.timer}>
          <View style={styles.timerTop}>
            <Text style={styles.timerLabel}>
              {backgroundEnabled ? 'چرخه بعدی' : 'چرخه (خاموش)'}
            </Text>
            <Gauge
              size={13}
              color={backgroundEnabled ? colors.accent : colors.textMute}
              strokeWidth={2.2}
            />
          </View>
          <Text
            style={[
              styles.timerValue,
              !backgroundEnabled && styles.timerValueMute,
            ]}
          >
            {testing
              ? '—'
              : backgroundEnabled
                ? formatCountdown(remainingMs)
                : 'دستی'}
          </Text>
        </View>
      </View>

      {testing ? (
        <View style={styles.progressCard}>
          <View style={styles.progressTop}>
            <Text style={styles.progressStatus} numberOfLines={1}>
              {progressStatus || 'تست…'}
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

      <View style={styles.modeBlock}>
        <Text style={styles.modeTitle}>تست‌ها (چندتایی)</Text>
        <View style={styles.modeSeg}>
          {TEST_FLAG_OPTIONS.map((opt) => {
            const active = testFlags[opt.id as keyof TestFlags];
            return (
              <Pressable
                key={opt.id}
                disabled={testing}
                onPress={() => void toggleTestFlag(opt.id as keyof TestFlags)}
                style={({ pressed }) => [
                  styles.modeChip,
                  active && styles.modeChipActive,
                  testing && styles.chipDisabled,
                  pressed && !testing && styles.chipPressed,
                ]}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                accessibilityLabel={opt.title}
              >
                <Text
                  style={[
                    styles.modeChipText,
                    active && styles.modeChipTextActive,
                  ]}
                  numberOfLines={1}
                >
                  {opt.title}
                </Text>
              </Pressable>
            );
          })}
        </View>
        <Text style={styles.modeHint} numberOfLines={2}>
          {modeHint}
        </Text>
      </View>

      <View style={styles.intervalBlock}>
        <View style={styles.intervalHead}>
          <Text style={styles.intervalTitle}>بازهٔ چرخه خودکار</Text>
          <Text style={styles.intervalSelected} numberOfLines={1}>
            هر {toFaDigits(cycleIntervalLabel(cycleIntervalMs))}
          </Text>
        </View>

        <Text style={styles.intervalGroupLabel}>دقیقه</Text>
        <View style={styles.chips}>
          {CYCLE_MINUTE_OPTIONS.map((opt) => {
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
                accessibilityLabel={opt.title}
              >
                <Text
                  style={[styles.chipText, active && styles.chipTextActive]}
                >
                  {toFaDigits(opt.short)}
                </Text>
              </Pressable>
            );
          })}
        </View>

        <Text style={styles.intervalGroupLabel}>ساعت</Text>
        <View style={styles.chips}>
          {CYCLE_HOUR_OPTIONS.map((opt) => {
            const active = opt.ms === cycleIntervalMs;
            return (
              <Pressable
                key={opt.ms}
                disabled={testing}
                onPress={() => onCycleInterval(opt.ms)}
                style={({ pressed }) => [
                  styles.chip,
                  styles.chipWide,
                  active && styles.chipActive,
                  testing && styles.chipDisabled,
                  pressed && !testing && styles.chipPressed,
                ]}
                accessibilityRole="button"
                accessibilityState={{ selected: active }}
                accessibilityLabel={opt.title}
              >
                <Text
                  style={[styles.chipText, active && styles.chipTextActive]}
                >
                  {toFaDigits(opt.short)}
                </Text>
                <Text
                  style={[
                    styles.chipSub,
                    active && styles.chipSubActive,
                  ]}
                >
                  {toFaDigits(opt.title)}
                </Text>
              </Pressable>
            );
          })}
        </View>

        <Text style={styles.intervalHint} numberOfLines={2}>
          {backgroundEnabled
            ? `تایمر دقیق: بعدی ${formatCountdown(remainingMs)} · الان ${toFaDigits(cycleOptionOf(cycleIntervalMs).title)}`
            : 'پایش پس‌زمینه را روشن کن تا چرخه با این بازه اجرا شود'}
        </Text>
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
  btnSlot: {
    flex: 1,
    minHeight: 64,
    position: 'relative',
  },
  btn: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 10,
    borderRadius: radii.lg,
    paddingVertical: 14,
    paddingHorizontal: 12,
    minHeight: 64,
  },
  btnTest: {
    backgroundColor: colors.accent,
  },
  btnCancel: {
    backgroundColor: '#E85D4C',
  },
  btnGone: {
    opacity: 0,
    position: 'absolute',
    left: 0,
    right: 0,
    top: 0,
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
    fontSize: 22,
    fontFamily: fonts.extraBold,
    fontVariant: ['tabular-nums'],
    letterSpacing: -0.3,
  },
  timerValueMute: {
    color: colors.textMute,
    fontSize: 18,
  },
  modeBlock: {
    gap: 8,
  },
  modeTitle: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.medium,
  },
  modeSeg: {
    flexDirection: 'row-reverse',
    gap: 6,
  },
  modeChip: {
    flex: 1,
    paddingVertical: 10,
    paddingHorizontal: 6,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    alignItems: 'center',
    justifyContent: 'center',
  },
  modeChipActive: {
    borderColor: 'rgba(42,212,197,0.55)',
    backgroundColor: colors.accentDim,
  },
  modeChipText: {
    color: colors.textSoft,
    fontSize: 12,
    fontFamily: fonts.bold,
    textAlign: 'center',
  },
  modeChipTextActive: {
    color: colors.accent,
    fontFamily: fonts.extraBold,
  },
  modeHint: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.medium,
    lineHeight: 16,
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
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.panel,
    padding: 10,
  },
  intervalHead: {
    flexDirection: 'row-reverse',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
  },
  intervalTitle: {
    textAlign: 'right',
    color: colors.textSoft,
    fontSize: 12,
    fontFamily: fonts.bold,
  },
  intervalSelected: {
    flexShrink: 1,
    textAlign: 'left',
    color: colors.accent,
    fontSize: 12,
    fontFamily: fonts.extraBold,
  },
  intervalGroupLabel: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 10,
    fontFamily: fonts.medium,
    marginTop: 2,
  },
  intervalHint: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 10,
    fontFamily: fonts.medium,
    lineHeight: 15,
    marginTop: 2,
  },
  chips: {
    flexDirection: 'row-reverse',
    flexWrap: 'wrap',
    gap: 6,
    justifyContent: 'flex-start',
  },
  chip: {
    minWidth: 46,
    paddingVertical: 9,
    paddingHorizontal: 10,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.bgElevated,
    alignItems: 'center',
    justifyContent: 'center',
  },
  chipWide: {
    minWidth: 64,
    paddingVertical: 8,
    gap: 2,
  },
  chipActive: {
    borderColor: 'rgba(42,212,197,0.55)',
    backgroundColor: colors.accentDim,
  },
  chipDisabled: { opacity: 0.45 },
  chipPressed: { opacity: 0.85 },
  chipText: {
    color: colors.textSoft,
    fontSize: 13,
    fontFamily: fonts.bold,
    fontVariant: ['tabular-nums'],
  },
  chipTextActive: {
    color: colors.accent,
    fontFamily: fonts.extraBold,
  },
  chipSub: {
    color: colors.textMute,
    fontSize: 9,
    fontFamily: fonts.medium,
  },
  chipSubActive: {
    color: 'rgba(42,212,197,0.85)',
  },
});
