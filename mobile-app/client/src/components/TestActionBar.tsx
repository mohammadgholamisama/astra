import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { Gauge, Zap } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';
import { formatCountdown } from '../utils/format';

export function TestActionBar({
  testing,
  disabled,
  remainingMs,
  onTest,
}: {
  testing: boolean;
  disabled: boolean;
  remainingMs: number;
  onTest: () => void;
}) {
  return (
    <View style={styles.wrap}>
      <Pressable
        disabled={testing || disabled}
        onPress={onTest}
        style={[styles.btn, (testing || disabled) && styles.btnDisabled]}
      >
        {testing ? (
          <ActivityIndicator color={colors.bg} />
        ) : (
          <>
            <View style={styles.btnCopy}>
              <Text style={styles.btnTitle}>تست الان</Text>
              <Text style={styles.btnHint}>پینگ زنده · هر کانفیگ جدا</Text>
            </View>
            <View style={styles.btnIcon}>
              <Zap size={18} color={colors.bg} strokeWidth={2.4} fill={colors.bg} />
            </View>
          </>
        )}
      </Pressable>

      <View style={styles.timer}>
        <View style={styles.timerTop}>
          <Text style={styles.timerLabel}>چرخه بعدی</Text>
          <Gauge size={13} color={colors.accent} strokeWidth={2.2} />
        </View>
        <Text style={styles.timerValue}>{formatCountdown(remainingMs)}</Text>
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
  btnDisabled: { backgroundColor: 'rgba(255,255,255,0.1)' },
  btnIcon: {
    width: 34,
    height: 34,
    borderRadius: 12,
    backgroundColor: 'rgba(5,8,15,0.12)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  btnCopy: { alignItems: 'flex-end' },
  btnTitle: { color: colors.bg, fontFamily: fonts.extraBold, fontSize: 16 },
  btnHint: {
    color: 'rgba(5,8,15,0.55)',
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
  timerLabel: { color: colors.textMute, fontSize: 10, fontFamily: fonts.medium },
  timerValue: {
    marginTop: 4,
    textAlign: 'right',
    alignSelf: 'stretch',
    color: colors.accent,
    fontSize: 26,
    fontFamily: fonts.extraBold,
    fontVariant: ['tabular-nums'],
  },
});
