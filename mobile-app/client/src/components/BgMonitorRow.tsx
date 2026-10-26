import React, { useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Switch,
  Text,
  View,
} from 'react-native';
import { MoonStar, ShieldCheck } from 'lucide-react-native';
import { colors, fonts, radii } from '../theme';

export function BgMonitorRow({
  enabled,
  onChange,
}: {
  enabled: boolean;
  onChange: (v: boolean) => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  const toggle = async (next: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      await onChange(next);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pressable
      onPress={() => void toggle(!enabled)}
      style={[styles.wrap, enabled && styles.wrapOn]}
    >
      {/* LTR row → سوییچ فیزیکی راست، متن راست‌چین کنارش */}
      <View style={styles.row}>
        <View style={styles.body}>
          <View style={styles.titleRow}>
            <View style={styles.iconWrap}>
              <MoonStar size={14} color={colors.accent} strokeWidth={2.2} />
            </View>
            <Text style={styles.title}>پایش پس‌زمینه</Text>
          </View>
          <Text style={styles.desc}>
            برای فعال‌سازی، دسترسی اعلان و کار در پس‌زمینه گرفته می‌شود
          </Text>
          {enabled ? (
            <View style={styles.okRow}>
              <ShieldCheck size={12} color={colors.mint} strokeWidth={2.4} />
              <Text style={styles.okText}>فعال · حتی با اپ بسته‌شده</Text>
            </View>
          ) : null}
        </View>

        {busy ? (
          <ActivityIndicator color={colors.accent} style={styles.switchSlot} />
        ) : (
          <Switch
            value={enabled}
            onValueChange={(v) => void toggle(v)}
            trackColor={{ false: '#2A3344', true: 'rgba(42,212,197,0.45)' }}
            thumbColor={enabled ? colors.accent : '#8A93A5'}
            style={styles.switchSlot}
          />
        )}
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.panel,
    paddingHorizontal: 14,
    paddingVertical: 14,
  },
  wrapOn: {
    borderColor: 'rgba(42,212,197,0.28)',
    backgroundColor: colors.accentSoft,
  },
  row: {
    direction: 'ltr',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 12,
  },
  body: {
    flex: 1,
    alignItems: 'flex-end',
  },
  titleRow: {
    direction: 'rtl',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  iconWrap: {
    width: 26,
    height: 26,
    borderRadius: 8,
    backgroundColor: colors.accentDim,
    alignItems: 'center',
    justifyContent: 'center',
  },
  title: {
    textAlign: 'right',
    color: colors.text,
    fontSize: 14,
    fontFamily: fonts.extraBold,
  },
  desc: {
    marginTop: 5,
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    lineHeight: 18,
    fontFamily: fonts.regular,
  },
  okRow: {
    marginTop: 8,
    flexDirection: 'row-reverse',
    alignItems: 'center',
    gap: 5,
  },
  okText: {
    color: colors.mint,
    fontSize: 11,
    fontFamily: fonts.medium,
  },
  switchSlot: {
    marginLeft: 2,
  },
});
