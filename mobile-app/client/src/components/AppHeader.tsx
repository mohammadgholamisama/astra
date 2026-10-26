import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors, fonts, radii } from '../theme';

export function AppHeader() {
  return (
    <View style={styles.wrap}>
      <View style={styles.brandBlock}>
        <Text style={styles.brand}>Astra Ping</Text>
        <View style={styles.dot} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    // Keep Latin brand on the visual LEFT even when app is RTL
    direction: 'ltr',
    width: '100%',
    marginTop: 6,
    marginBottom: 18,
    alignItems: 'flex-start',
  },
  brandBlock: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 10,
  },
  brand: {
    textAlign: 'left',
    writingDirection: 'ltr',
    color: colors.text,
    fontSize: 28,
    fontFamily: fonts.extraBold,
    letterSpacing: -0.4,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: radii.pill,
    backgroundColor: colors.accent,
  },
});
