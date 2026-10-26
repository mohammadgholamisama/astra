import React, { useEffect } from 'react';
import { I18nManager, StatusBar, StyleSheet, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { MainDashboard } from './src/screens/MainDashboard';

I18nManager.allowRTL(true);
I18nManager.forceRTL(true);

function App(): React.JSX.Element {
  useEffect(() => {
    StatusBar.setBarStyle('light-content');
  }, []);

  return (
    <SafeAreaProvider>
      <View style={styles.root}>
        <StatusBar barStyle="light-content" backgroundColor="#05080F" />
        <MainDashboard />
      </View>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  // Yoga RTL: row starts from the right; flex-start = right edge
  root: { flex: 1, direction: 'rtl' },
});

export default App;
