import React from 'react';
import { Image, StyleSheet, View } from 'react-native';
import type { Operator } from '../../types';

const LOGOS = {
  irancell: require('../../assets/operators/irancell.png'),
  mci: require('../../assets/operators/mci.png'),
  rightel: require('../../assets/operators/rightel.png'),
} as const;

export function OperatorIcon({
  id,
  size = 28,
}: {
  id: Operator;
  size?: number;
}) {
  return (
    <View style={[styles.box, { width: size, height: size }]}>
      <Image
        source={LOGOS[id]}
        style={{ width: size, height: size }}
        resizeMode="contain"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  box: {
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: 'transparent',
  },
});
