import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors, fonts, radii } from '../theme';

export function AppHeader() {
  return (
    <View style={styles.wrap}>
      <View style={styles.brandBlock}>
        <Text style={styles.brand}>پینگ واقعی</Text>
        <View style={styles.dot} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
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
    textAlign: 'right',
    color: colors.text,
    fontSize: 28,
    fontFamily: fonts.extraBold,
    letterSpacing: -0.3,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: radii.pill,
    backgroundColor: colors.accent,
  },
});
