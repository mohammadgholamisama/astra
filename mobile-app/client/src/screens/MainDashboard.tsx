import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Activity } from 'lucide-react-native';
import { AppHeader } from '../components/AppHeader';
import { ConfigCard } from '../components/ConfigCard';
import { OperatorPicker } from '../components/OperatorPicker';
import { SubLinkCard } from '../components/SubLinkCard';
import { BgMonitorRow } from '../components/BgMonitorRow';
import { TestActionBar } from '../components/TestActionBar';
import { useAppStore } from '../store/appStore';
import { toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

export function MainDashboard() {
  const insets = useSafeAreaInsets();
  const [loadingSub, setLoadingSub] = useState(false);
  const {
    hydrate,
    hydrated,
    operator,
    setOperator,
    subUrl,
    setSubUrl,
    ranked,
    configs,
    testing,
    testingHashes,
    progressDone,
    progressTotal,
    remainingMs,
    status,
    error,
    tick,
    loadSub,
    runCycle,
    backgroundEnabled,
    setBackgroundEnabled,
  } = useAppStore();

  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  useEffect(() => {
    const id = setInterval(() => tick(), 400);
    return () => clearInterval(id);
  }, [tick]);

  const progressPct = useMemo(() => {
    if (!progressTotal) return 0;
    return Math.round((progressDone / progressTotal) * 100);
  }, [progressDone, progressTotal]);

  const aliveCount = useMemo(
    () => ranked.filter((r) => r.avg_ping > 0).length,
    [ranked],
  );

  const onLoadSub = async () => {
    setLoadingSub(true);
    try {
      await loadSub();
    } finally {
      setLoadingSub(false);
    }
  };

  if (!hydrated) {
    return (
      <View style={[styles.root, styles.center]}>
        <ActivityIndicator color={colors.accent} size="large" />
        <Text style={styles.bootText}>در حال آماده‌سازی…</Text>
      </View>
    );
  }

  return (
    <View style={[styles.root, { paddingTop: insets.top }]}>
      <View pointerEvents="none" style={StyleSheet.absoluteFill}>
        <View style={[styles.glow, styles.glowA]} />
        <View style={[styles.glow, styles.glowB]} />
      </View>

      <ScrollView
        contentContainerStyle={{
          paddingBottom: insets.bottom + 36,
          paddingHorizontal: 16,
        }}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        <AppHeader />

        <View style={styles.stack}>
          <OperatorPicker value={operator} onChange={setOperator} />

          <SubLinkCard
            value={subUrl}
            onChange={setSubUrl}
            onLoad={() => void onLoadSub()}
            loading={loadingSub}
            count={configs.length}
          />

          <BgMonitorRow
            enabled={backgroundEnabled}
            onChange={(v) => void setBackgroundEnabled(v)}
          />

          <TestActionBar
            testing={testing}
            disabled={!operator}
            remainingMs={remainingMs}
            onTest={() => void runCycle(true)}
          />
        </View>

        {testing && (
          <View style={styles.progressCard}>
            <View style={styles.progressTop}>
              <Text style={styles.progressStatus} numberOfLines={1}>
                {status}
              </Text>
              <Text style={styles.progressPct}>{toFaDigits(progressPct)}٪</Text>
            </View>
            <View style={styles.progressTrack}>
              <View style={[styles.progressFill, { width: `${progressPct}%` }]} />
            </View>
            <Text style={styles.progressMeta}>
              {toFaDigits(progressDone)} از {toFaDigits(progressTotal)} · نمایش لحظه‌ای
            </Text>
          </View>
        )}

        {!testing && !!status && <Text style={styles.idleStatus}>{status}</Text>}

        {error ? (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>{error}</Text>
          </View>
        ) : null}

        <View style={styles.listHeader}>
          <View style={styles.listTitleRow}>
            <Text style={styles.listTitle}>رتبه‌بندی زنده</Text>
            <Activity size={16} color={colors.accent} strokeWidth={2.2} />
          </View>
          <View style={styles.listStats}>
            <Text style={styles.listStat}>{toFaDigits(ranked.length)} کل</Text>
            {aliveCount > 0 && (
              <View style={styles.alivePill}>
                <Text style={styles.aliveText}>{toFaDigits(aliveCount)} فعال</Text>
              </View>
            )}
          </View>
        </View>

        {ranked.length === 0 ? (
          <View style={styles.empty}>
            <View style={styles.emptyIcon}>
              <Activity size={22} color={colors.accent} strokeWidth={2} />
            </View>
            <Text style={styles.emptyTitle}>هنوز کانفیگی نیست</Text>
            <Text style={styles.emptyText}>
              اپراتور را انتخاب کن، ساب را بارگذاری کن، بعد «تست الان» را بزن.
            </Text>
          </View>
        ) : (
          ranked.map((item) => (
            <ConfigCard
              key={item.config_hash}
              item={item}
              highlight={testingHashes.includes(item.config_hash)}
            />
          ))
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  center: { alignItems: 'center', justifyContent: 'center', gap: 12 },
  bootText: {
    color: colors.textMute,
    fontSize: 13,
    textAlign: 'right',
    fontFamily: fonts.regular,
  },
  glow: { position: 'absolute', width: 280, height: 280, borderRadius: 140 },
  glowA: { top: -90, right: -80, backgroundColor: 'rgba(42,212,197,0.07)' },
  glowB: { top: 360, left: -110, backgroundColor: 'rgba(0,163,224,0.05)' },

  stack: { gap: 12, marginBottom: 4 },

  progressCard: {
    marginTop: 12,
    marginBottom: 8,
    borderRadius: radii.lg,
    backgroundColor: colors.panel,
    borderWidth: 1,
    borderColor: colors.line,
    padding: 14,
  },
  progressTop: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 10,
    gap: 8,
  },
  progressStatus: {
    flex: 1,
    textAlign: 'right',
    color: colors.textSoft,
    fontSize: 12,
    fontFamily: fonts.regular,
  },
  progressPct: { color: colors.mint, fontFamily: fonts.extraBold, fontSize: 14 },
  progressTrack: {
    height: 7,
    borderRadius: 99,
    backgroundColor: colors.bg,
    overflow: 'hidden',
  },
  progressFill: { height: '100%', borderRadius: 99, backgroundColor: colors.mint },
  progressMeta: {
    marginTop: 8,
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 11,
  },
  idleStatus: {
    marginTop: 10,
    marginBottom: 4,
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 12,
  },
  errorBox: {
    marginTop: 10,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: 'rgba(251,113,133,0.35)',
    backgroundColor: 'rgba(251,113,133,0.1)',
    padding: 12,
  },
  errorText: {
    textAlign: 'right',
    color: colors.rose,
    fontSize: 13,
    lineHeight: 20,
  },

  listHeader: {
    marginTop: 18,
    marginBottom: 12,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  listTitleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 8,
  },
  listTitle: {
    textAlign: 'right',
    color: colors.text,
    fontSize: 17,
    fontFamily: fonts.extraBold,
  },
  listStats: { flexDirection: 'row', gap: 8, alignItems: 'center' },
  listStat: { color: colors.textMute, fontSize: 12, fontFamily: fonts.medium },
  alivePill: {
    backgroundColor: 'rgba(74,222,128,0.12)',
    borderRadius: radii.pill,
    paddingHorizontal: 8,
    paddingVertical: 3,
  },
  aliveText: { color: colors.mint, fontSize: 11, fontFamily: fonts.bold },

  empty: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.lineStrong,
    backgroundColor: colors.bgElevated,
    paddingHorizontal: 22,
    paddingVertical: 36,
    alignItems: 'center',
  },
  emptyIcon: {
    width: 48,
    height: 48,
    borderRadius: 24,
    backgroundColor: colors.accentDim,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 12,
  },
  emptyTitle: {
    textAlign: 'center',
    color: colors.text,
    fontSize: 15,
    fontFamily: fonts.bold,
    marginBottom: 6,
  },
  emptyText: {
    textAlign: 'center',
    color: colors.textMute,
    fontSize: 13,
    lineHeight: 21,
    fontFamily: fonts.regular,
  },
});
