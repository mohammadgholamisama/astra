import React, { useMemo, useState } from 'react';
import {
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { Check, Copy, X } from 'lucide-react-native';
import type { ParsedConfig, RankedConfig } from '../types';
import { formatMs, toFaDigits } from '../utils/format';
import { copyToClipboard } from '../utils/clipboard';
import { colors, fonts, radii } from '../theme';

function Row({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <View style={styles.row}>
      <Text style={styles.value} selectable>
        {value}
      </Text>
      <Text style={styles.label}>{label}</Text>
    </View>
  );
}

export function ConfigDetailModal({
  visible,
  ranked,
  parsed,
  onClose,
}: {
  visible: boolean;
  ranked: RankedConfig | null;
  parsed: ParsedConfig | null;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const raw = parsed?.raw || ranked?.raw || '';
  const rows = useMemo(() => {
    if (!ranked && !parsed) return [];
    const ping =
      ranked && ranked.avg_ping > 0
        ? formatMs(ranked.avg_ping)
        : ranked?.measured
          ? ranked.avg_ping < 0
            ? 'قطع'
            : formatMs(ranked.avg_ping)
          : '—';
    return [
      { label: 'نام', value: ranked?.remark || parsed?.remark || '—' },
      { label: 'رتبه', value: ranked ? toFaDigits(ranked.rank) : '—' },
      { label: 'پینگ', value: ping },
      { label: 'پروتکل', value: (ranked?.protocol || parsed?.protocol || '').toUpperCase() },
      { label: 'ترنسپورت', value: ranked?.transport || parsed?.transport || '' },
      { label: 'امنیت', value: ranked?.security || parsed?.security || '' },
      { label: 'آدرس', value: ranked?.host || parsed?.address || '' },
      {
        label: 'پورت',
        value: String(ranked?.port || parsed?.port || ''),
      },
      { label: 'SNI / Host', value: parsed?.sni || ranked?.match_host || '' },
      { label: 'Path', value: parsed?.path || '' },
      { label: 'UUID', value: parsed?.uuid || '' },
      {
        label: 'هش',
        value: (ranked?.config_hash || parsed?.configHash || '').slice(0, 16),
      },
    ].filter((r) => r.value && r.value !== '—');
  }, [ranked, parsed]);

  const onCopy = async () => {
    if (!raw) return;
    const ok = await copyToClipboard(raw);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    }
  };

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={onClose}
    >
      <Pressable style={styles.backdrop} onPress={onClose}>
        <Pressable style={styles.sheet} onPress={(e) => e.stopPropagation()}>
          <View style={styles.head}>
            <Pressable onPress={onClose} hitSlop={10} style={styles.closeBtn}>
              <X size={18} color={colors.textSoft} strokeWidth={2.4} />
            </Pressable>
            <Text style={styles.headTitle} numberOfLines={2}>
              {ranked?.remark || parsed?.remark || 'جزئیات کانفیگ'}
            </Text>
          </View>

          <ScrollView
            style={styles.scroll}
            contentContainerStyle={styles.scrollContent}
            showsVerticalScrollIndicator={false}
          >
            {rows.map((r) => (
              <Row key={r.label} label={r.label} value={r.value} />
            ))}

            {!!raw && (
              <View style={styles.rawBox}>
                <Text style={styles.rawLabel}>لینک کامل</Text>
                <Text style={styles.rawText} selectable>
                  {raw}
                </Text>
              </View>
            )}
          </ScrollView>

          <View style={styles.actions}>
            <Pressable
              onPress={() => void onCopy()}
              disabled={!raw}
              style={[styles.copyBtn, !raw && styles.copyDisabled]}
            >
              {copied ? (
                <Check size={18} color={colors.bg} strokeWidth={2.6} />
              ) : (
                <Copy size={18} color={colors.bg} strokeWidth={2.4} />
              )}
              <Text style={styles.copyText}>
                {copied ? 'کپی شد' : 'کپی کانفیگ'}
              </Text>
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.62)',
    justifyContent: 'flex-end',
    padding: 14,
  },
  sheet: {
    direction: 'ltr',
    maxHeight: '82%',
    borderRadius: radii.xl,
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.lineStrong,
    overflow: 'hidden',
  },
  head: {
    flexDirection: 'row-reverse',
    alignItems: 'center',
    gap: 10,
    paddingHorizontal: 14,
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.line,
  },
  headTitle: {
    flex: 1,
    textAlign: 'right',
    writingDirection: 'rtl',
    color: colors.text,
    fontSize: 16,
    fontFamily: fonts.extraBold,
  },
  closeBtn: {
    width: 34,
    height: 34,
    borderRadius: 12,
    backgroundColor: colors.panel,
    alignItems: 'center',
    justifyContent: 'center',
  },
  scroll: { flexGrow: 0 },
  scrollContent: { padding: 14, gap: 8, paddingBottom: 8 },
  row: {
    flexDirection: 'row-reverse',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 12,
    paddingVertical: 8,
    paddingHorizontal: 10,
    borderRadius: radii.md,
    backgroundColor: colors.panel,
    borderWidth: 1,
    borderColor: colors.line,
  },
  label: {
    color: colors.textMute,
    fontSize: 12,
    fontFamily: fonts.medium,
    textAlign: 'right',
  },
  value: {
    flex: 1,
    color: colors.text,
    fontSize: 13,
    fontFamily: fonts.bold,
    textAlign: 'left',
    writingDirection: 'ltr',
  },
  rawBox: {
    marginTop: 6,
    borderRadius: radii.md,
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderColor: colors.line,
    padding: 12,
  },
  rawLabel: {
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.medium,
    marginBottom: 8,
  },
  rawText: {
    color: colors.textSoft,
    fontSize: 11,
    lineHeight: 18,
    fontFamily: fonts.regular,
    writingDirection: 'ltr',
    textAlign: 'left',
  },
  actions: {
    padding: 14,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line,
  },
  copyBtn: {
    flexDirection: 'row-reverse',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    backgroundColor: colors.accent,
    borderRadius: radii.lg,
    paddingVertical: 14,
  },
  copyDisabled: { opacity: 0.45 },
  copyText: {
    color: colors.bg,
    fontSize: 15,
    fontFamily: fonts.extraBold,
  },
});
