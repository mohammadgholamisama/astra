import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  StyleSheet,
  Text,
  View,
  type ListRenderItem,
} from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { ConfigCard, CONFIG_ROW_HEIGHT } from '../components/ConfigCard';
import { ConfigDetailModal } from '../components/ConfigDetailModal';
import { AppHeader } from '../components/AppHeader';
import { SubLinkCard } from '../components/SubLinkCard';
import { BgMonitorRow } from '../components/BgMonitorRow';
import { TestActionBar } from '../components/TestActionBar';
import { PanelSyncButton } from '../components/PanelSyncButton';
import { useAppStore } from '../store/appStore';
import type { ParsedConfig, RankedConfig } from '../types';
import { toFaDigits } from '../utils/format';
import { colors, fonts, radii } from '../theme';

export function MainDashboard() {
  const insets = useSafeAreaInsets();
  const [loadingSub, setLoadingSub] = useState(false);
  const [detail, setDetail] = useState<{
    ranked: RankedConfig;
    parsed: ParsedConfig | null;
  } | null>(null);

  const hydrate = useAppStore((s) => s.hydrate);
  const hydrated = useAppStore((s) => s.hydrated);
  const subUrl = useAppStore((s) => s.subUrl);
  const setSubUrl = useAppStore((s) => s.setSubUrl);
  const ranked = useAppStore((s) => s.ranked);
  const configs = useAppStore((s) => s.configs);
  const testing = useAppStore((s) => s.testing);
  const testingHashes = useAppStore((s) => s.testingHashes);
  const progressDone = useAppStore((s) => s.progressDone);
  const progressTotal = useAppStore((s) => s.progressTotal);
  const remainingMs = useAppStore((s) => s.remainingMs);
  const cycleIntervalMs = useAppStore((s) => s.cycleIntervalMs);
  const status = useAppStore((s) => s.status);
  const error = useAppStore((s) => s.error);
  const tick = useAppStore((s) => s.tick);
  const loadSub = useAppStore((s) => s.loadSub);
  const runCycle = useAppStore((s) => s.runCycle);
  const cancelCycle = useAppStore((s) => s.cancelCycle);
  const setCycleInterval = useAppStore((s) => s.setCycleInterval);
  const backgroundEnabled = useAppStore((s) => s.backgroundEnabled);
  const setBackgroundEnabled = useAppStore((s) => s.setBackgroundEnabled);
  const syncingPanel = useAppStore((s) => s.syncingPanel);
  const syncPanelOrder = useAppStore((s) => s.syncPanelOrder);

  useEffect(() => {
    void hydrate();
  }, [hydrate]);

  useEffect(() => {
    const id = setInterval(() => tick(), 1000);
    return () => clearInterval(id);
  }, [tick]);

  const aliveCount = useMemo(
    () => ranked.reduce((n, r) => (r.avg_ping > 0 ? n + 1 : n), 0),
    [ranked],
  );

  const testingSet = useMemo(() => new Set(testingHashes), [testingHashes]);

  const byHash = useMemo(() => {
    const m = new Map<string, ParsedConfig>();
    for (const c of configs) m.set(c.configHash, c);
    return m;
  }, [configs]);

  const onLoadSub = useCallback(async () => {
    setLoadingSub(true);
    try {
      await loadSub();
    } finally {
      setLoadingSub(false);
    }
  }, [loadSub]);

  const onTest = useCallback(() => {
    void runCycle(true);
  }, [runCycle]);

  // Instant — cancel on press-in so it works even when JS is busy
  const onCancel = useCallback(() => {
    cancelCycle();
  }, [cancelCycle]);

  const onCycleInterval = useCallback(
    (ms: number) => {
      void setCycleInterval(ms);
    },
    [setCycleInterval],
  );

  const onSync = useCallback(() => {
    void syncPanelOrder();
  }, [syncPanelOrder]);

  const openDetail = useCallback(
    (item: RankedConfig) => {
      setDetail({
        ranked: item,
        parsed: byHash.get(item.config_hash) ?? null,
      });
    },
    [byHash],
  );

  const renderItem: ListRenderItem<RankedConfig> = useCallback(
    ({ item }) => (
      <ConfigCard
        item={item}
        highlight={testingSet.has(item.config_hash)}
        onDetails={() => openDetail(item)}
      />
    ),
    [openDetail, testingSet],
  );

  const keyExtractor = useCallback((item: RankedConfig) => item.config_hash, []);

  const getItemLayout = useCallback(
    (_: unknown, index: number) => ({
      length: CONFIG_ROW_HEIGHT + 6,
      offset: (CONFIG_ROW_HEIGHT + 6) * index,
      index,
    }),
    [],
  );

  // Only list chrome in FlatList header — controls stay OUTSIDE so they
  // never remount while ranked[] updates during a test.
  const listHeader = useMemo(
    () => (
      <View>
        {!testing && status ? (
          <Text style={styles.idleStatus}>{status}</Text>
        ) : null}

        {error ? (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>{error}</Text>
          </View>
        ) : null}

        <View style={styles.listHeader}>
          <Text style={styles.listStat}>
            {toFaDigits(ranked.length)} کل
            {aliveCount > 0 ? ` · ${toFaDigits(aliveCount)} فعال` : ''}
          </Text>
          <Text style={styles.listTitle}>رتبه‌بندی</Text>
        </View>
      </View>
    ),
    [testing, status, error, ranked.length, aliveCount],
  );

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
      {/* Sticky controls — outside FlatList so cancel always receives taps */}
      <View style={styles.controls}>
        <AppHeader />
        <View style={styles.stack}>
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
            disabled={false}
            remainingMs={remainingMs}
            cycleIntervalMs={cycleIntervalMs}
            progressDone={progressDone}
            progressTotal={progressTotal}
            progressStatus={status}
            onTest={onTest}
            onCancel={onCancel}
            onCycleInterval={onCycleInterval}
          />
          <PanelSyncButton
            busy={syncingPanel}
            disabled={testing}
            onPress={onSync}
          />
        </View>
      </View>

      <FlatList
        style={styles.list}
        data={ranked}
        keyExtractor={keyExtractor}
        renderItem={renderItem}
        getItemLayout={getItemLayout}
        ListHeaderComponent={listHeader}
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTitle}>هنوز کانفیگی نیست</Text>
            <Text style={styles.emptyText}>
              لینک ساب را بارگذاری کن، بعد «تست الان» را بزن.
            </Text>
          </View>
        }
        contentContainerStyle={{
          paddingBottom: insets.bottom + 28,
          paddingHorizontal: 14,
        }}
        initialNumToRender={10}
        maxToRenderPerBatch={6}
        windowSize={5}
        updateCellsBatchingPeriod={80}
        removeClippedSubviews
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      />

      <ConfigDetailModal
        visible={!!detail}
        ranked={detail?.ranked ?? null}
        parsed={detail?.parsed ?? null}
        onClose={() => setDetail(null)}
      />
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
  controls: {
    paddingHorizontal: 14,
    paddingBottom: 4,
    zIndex: 10,
    elevation: 10,
  },
  stack: { gap: 10, marginBottom: 4 },
  list: { flex: 1 },
  idleStatus: {
    marginTop: 8,
    marginBottom: 2,
    textAlign: 'right',
    color: colors.textMute,
    fontSize: 12,
  },
  errorBox: {
    marginTop: 8,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: 'rgba(251,113,133,0.35)',
    backgroundColor: 'rgba(251,113,133,0.1)',
    padding: 10,
  },
  errorText: {
    textAlign: 'right',
    color: colors.rose,
    fontSize: 13,
    lineHeight: 20,
  },
  listHeader: {
    marginTop: 14,
    marginBottom: 8,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  listTitle: {
    textAlign: 'right',
    color: colors.text,
    fontSize: 16,
    fontFamily: fonts.extraBold,
  },
  listStat: { color: colors.textMute, fontSize: 12, fontFamily: fonts.medium },
  empty: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.lineStrong,
    backgroundColor: colors.bgElevated,
    paddingHorizontal: 20,
    paddingVertical: 28,
    alignItems: 'center',
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
