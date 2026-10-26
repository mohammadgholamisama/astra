import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { ArrowUpDown } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';

export function PanelSyncButton({
  busy,
  disabled,
  onPress,
}: {
  busy: boolean;
  disabled?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      disabled={busy || disabled}
      onPress={onPress}
      style={[styles.btn, (busy || disabled) && styles.btnDisabled]}
    >
      {/* Parent app is RTL: first child = RIGHT side */}
      <View style={styles.row}>
        <View style={styles.copy}>
          <Text style={styles.title}>اعمال ترتیب روی پنل</Text>
          <Text style={styles.hint}>
            میانگین همین صفحه · هاست/پورت/دامنه · فقط موفق‌ها جابه‌جا می‌شوند
          </Text>
        </View>
        {busy ? (
          <ActivityIndicator color={colors.accent} />
        ) : (
          <View style={styles.iconWrap}>
            <ArrowUpDown size={18} color={colors.accent} strokeWidth={2.3} />
          </View>
        )}
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  btn: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: 'rgba(42,212,197,0.28)',
    backgroundColor: colors.bgElevated,
    paddingVertical: 14,
    paddingHorizontal: 14,
    minHeight: 64,
  },
  btnDisabled: { opacity: 0.55 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
    width: '100%',
  },
  iconWrap: {
    width: 40,
    height: 40,
    borderRadius: 14,
    backgroundColor: 'rgba(42,212,197,0.12)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  copy: { flex: 1, alignItems: 'flex-start' },
  title: {
    color: colors.text,
    fontFamily: fonts.bold,
    fontSize: 14,
    textAlign: 'right',
    width: '100%',
  },
  hint: {
    marginTop: 4,
    color: colors.textMute,
    fontFamily: fonts.regular,
    fontSize: 11,
    lineHeight: 17,
    textAlign: 'right',
    width: '100%',
  },
});
