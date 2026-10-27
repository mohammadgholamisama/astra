import React, { useRef } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { ArrowUpDown, CheckCircle2, AlertCircle } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';
import { useAppStore } from '../store/appStore';

export function PanelSyncButton({
  disabled,
  onPress,
}: {
  disabled?: boolean;
  onPress: () => void;
}) {
  const busy = useAppStore((s) => s.syncingPanel);
  const hint = useAppStore((s) => s.panelSyncHint);
  const lock = useRef(false);

  const okHint = hint.startsWith('اعمال شد');
  const errHint =
    !!hint &&
    !busy &&
    !okHint &&
    (hint.includes('ناموفق') ||
      hint.includes('خطا') ||
      hint.includes('منطبق') ||
      hint.includes('اول تست'));

  const handlePress = () => {
    if (busy || disabled || lock.current) return;
    lock.current = true;
    onPress();
    setTimeout(() => {
      lock.current = false;
    }, 1200);
  };

  return (
    <Pressable
      disabled={busy || disabled}
      onPress={handlePress}
      style={[styles.btn, (busy || disabled) && styles.btnDisabled]}
    >
      <View style={styles.row}>
        <View style={styles.copy}>
          <Text style={styles.title}>
            {busy ? 'در حال اعمال روی پنل…' : 'اعمال ترتیب روی پنل'}
          </Text>
          <Text
            style={[
              styles.hint,
              okHint && styles.hintOk,
              errHint && styles.hintErr,
            ]}
          >
            {busy
              ? hint || 'صبر کن — اسپینر تا پایان درخواست می‌چرخد'
              : hint ||
                'میانگین ۱۵د · هاست/پورت/دامنه · فقط موفق‌ها جابه‌جا می‌شوند'}
          </Text>
        </View>
        {busy ? (
          <ActivityIndicator color={colors.accent} />
        ) : okHint ? (
          <View style={[styles.iconWrap, styles.iconOk]}>
            <CheckCircle2 size={18} color={colors.mint} strokeWidth={2.3} />
          </View>
        ) : errHint ? (
          <View style={[styles.iconWrap, styles.iconErr]}>
            <AlertCircle size={18} color={colors.rose} strokeWidth={2.3} />
          </View>
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
  iconOk: { backgroundColor: 'rgba(42,212,197,0.18)' },
  iconErr: { backgroundColor: 'rgba(251,113,133,0.14)' },
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
  hintOk: { color: colors.mint },
  hintErr: { color: colors.rose },
});
