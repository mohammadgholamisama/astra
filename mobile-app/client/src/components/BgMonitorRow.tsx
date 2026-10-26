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
      {/*
        Force visual LTR so layout is stable regardless of app RTL:
        [ Switch LEFT ] ........ [ text + icon RIGHT ]
      */}
      <View style={styles.row}>
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

        <View style={styles.body}>
          <View style={styles.titleRow}>
            <Text style={styles.title}>پایش پس‌زمینه</Text>
            <View style={styles.iconWrap}>
              <MoonStar size={14} color={colors.accent} strokeWidth={2.2} />
            </View>
          </View>
          <Text style={styles.desc}>
            برای فعال‌سازی، دسترسی اعلان و کار در پس‌زمینه گرفته می‌شود
          </Text>
          {enabled ? (
            <View style={styles.okRow}>
              <Text style={styles.okText}>فعال · حتی با اپ بسته‌شده</Text>
              <ShieldCheck size={12} color={colors.mint} strokeWidth={2.4} />
            </View>
          ) : null}
        </View>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  wrap: {
    direction: 'ltr',
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
    width: '100%',
  },
  switchSlot: {
    flexShrink: 0,
  },
  body: {
    flex: 1,
    alignItems: 'flex-end',
  },
  titleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'flex-end',
    gap: 8,
    alignSelf: 'stretch',
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
    writingDirection: 'rtl',
    color: colors.text,
    fontSize: 14,
    fontFamily: fonts.extraBold,
  },
  desc: {
    marginTop: 5,
    textAlign: 'right',
    writingDirection: 'rtl',
    alignSelf: 'stretch',
    color: colors.textMute,
    fontSize: 11,
    lineHeight: 18,
    fontFamily: fonts.regular,
  },
  okRow: {
    marginTop: 8,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'flex-end',
    gap: 5,
    alignSelf: 'stretch',
  },
  okText: {
    textAlign: 'right',
    writingDirection: 'rtl',
    color: colors.mint,
    fontSize: 11,
    fontFamily: fonts.medium,
  },
});
