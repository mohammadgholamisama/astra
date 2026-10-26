import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { OPERATORS, type Operator } from '../types';
import { colors, fonts, radii } from '../theme';
import { OperatorIcon } from './icons/OperatorIcons';

export function OperatorPicker({
  value,
  onChange,
}: {
  value: Operator | null;
  onChange: (op: Operator) => void;
}) {
  return (
    <View style={styles.wrap}>
      <Text style={styles.label}>اپراتور اینترنت</Text>
      <View style={styles.row}>
        {OPERATORS.map((op) => {
          const active = value === op.id;
          return (
            <Pressable
              key={op.id}
              onPress={() => onChange(op.id)}
              style={[
                styles.chip,
                active && {
                  borderColor: op.color,
                  backgroundColor: `${op.color}12`,
                },
              ]}
            >
              <OperatorIcon id={op.id} size={40} />
              <Text style={[styles.name, active && { color: colors.text }]}>{op.label}</Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.bgElevated,
    padding: 14,
  },
  label: {
    textAlign: 'right',
    color: colors.textSoft,
    fontSize: 13,
    fontFamily: fonts.bold,
    marginBottom: 12,
  },
  row: { flexDirection: 'row', gap: 8 },
  chip: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    borderRadius: radii.lg,
    borderWidth: 1.5,
    borderColor: colors.lineStrong,
    backgroundColor: colors.panel,
    paddingVertical: 12,
    paddingHorizontal: 4,
    minHeight: 96,
  },
  name: {
    color: colors.textMute,
    fontSize: 11,
    fontFamily: fonts.bold,
    textAlign: 'center',
  },
});
