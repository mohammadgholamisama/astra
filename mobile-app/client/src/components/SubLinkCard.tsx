import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { ClipboardPaste, Link2 } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';
import { toFaDigits } from '../utils/format';

export function SubLinkCard({
  value,
  onChange,
  onLoad,
  loading,
  count,
}: {
  value: string;
  onChange: (v: string) => void;
  onLoad: () => void;
  loading?: boolean;
  count: number;
}) {
  return (
    <View style={styles.wrap}>
      <View style={styles.head}>
        <Text style={styles.label}>لینک ساب</Text>
        <View style={styles.countBadge}>
          <Text style={styles.countText}>{toFaDigits(count)} کانفیگ</Text>
        </View>
      </View>

      {/* LTR field: icon · url · action — cleaner for URLs */}
      <View style={styles.field}>
        <View style={styles.linkBadge}>
          <Link2 size={16} color={colors.accent} strokeWidth={2.2} />
        </View>
        <TextInput
          value={value}
          onChangeText={onChange}
          placeholder="https://…/sub/…"
          placeholderTextColor="rgba(245,247,251,0.28)"
          autoCapitalize="none"
          autoCorrect={false}
          style={styles.input}
          textAlign="left"
          numberOfLines={1}
        />
        <Pressable onPress={onLoad} disabled={loading} hitSlop={6} style={styles.sideBtn}>
          <ClipboardPaste size={16} color={colors.accent} strokeWidth={2.2} />
        </Pressable>
      </View>

      <Pressable
        onPress={onLoad}
        disabled={loading}
        style={[styles.btn, loading && styles.btnDisabled]}
      >
        {loading ? (
          <ActivityIndicator color={colors.bg} />
        ) : (
          <Text style={styles.btnText}>بارگذاری ساب</Text>
        )}
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.bgElevated,
    padding: 14,
    gap: 12,
  },
  head: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  label: {
    textAlign: 'right',
    color: colors.text,
    fontSize: 14,
    fontFamily: fonts.extraBold,
  },
  countBadge: {
    borderRadius: radii.pill,
    backgroundColor: colors.panel,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  countText: { color: colors.textSoft, fontSize: 11, fontFamily: fonts.bold },
  field: {
    direction: 'ltr',
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    minHeight: 50,
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 8,
    gap: 6,
  },
  linkBadge: {
    width: 32,
    height: 32,
    borderRadius: 10,
    backgroundColor: colors.accentDim,
    alignItems: 'center',
    justifyContent: 'center',
  },
  input: {
    flex: 1,
    color: colors.text,
    fontSize: 12,
    paddingVertical: 10,
    paddingHorizontal: 2,
    fontFamily: fonts.regular,
  },
  sideBtn: {
    width: 34,
    height: 34,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
  },
  btn: {
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: radii.lg,
    backgroundColor: colors.accent,
    paddingVertical: 13,
  },
  btnDisabled: { opacity: 0.55 },
  btnText: { color: colors.bg, fontFamily: fonts.extraBold, fontSize: 14 },
});
