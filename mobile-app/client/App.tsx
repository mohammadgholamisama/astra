import React, { useEffect, useRef } from 'react';
import {
  AppState,
  type AppStateStatus,
  I18nManager,
  StatusBar,
  StyleSheet,
  View,
} from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { MainDashboard } from './src/screens/MainDashboard';
import { ErrorBoundary } from './src/components/ErrorBoundary';
import { useAppStore } from './src/store/appStore';
import { saveSnapshot } from './src/store/persist';

try {
  I18nManager.allowRTL(true);
} catch {
  /* ignore */
}

function App(): React.JSX.Element {
  const appState = useRef(AppState.currentState);

  useEffect(() => {
    StatusBar.setBarStyle('light-content');
  }, []);

  // Persist on leave — do NOT cancel the running test (that was cutting cycles short).
  useEffect(() => {
    const onChange = (next: AppStateStatus) => {
      const prev = appState.current;
      appState.current = next;
      const leaving =
        prev === 'active' &&
        (next === 'inactive' || next === 'background');
      if (!leaving) return;

      const { configs, ranked, subUrl, backgroundEnabled, testing } =
        useAppStore.getState();
      if (configs.length > 0) {
        void saveSnapshot(configs, ranked, {
          subUrl,
          backgroundEnabled,
          lastBackgroundStatus: testing
            ? 'تست در پس‌زمینه ادامه دارد…'
            : undefined,
        });
      }
    };
    const sub = AppState.addEventListener('change', onChange);
    return () => sub.remove();
  }, []);

  return (
    <SafeAreaProvider>
      <ErrorBoundary>
        <View style={styles.root}>
          <StatusBar barStyle="light-content" backgroundColor="#05080F" />
          <MainDashboard />
        </View>
      </ErrorBoundary>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#05080F', direction: 'rtl' },
});

export default App;
