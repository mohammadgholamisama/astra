import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { colors, fonts, radii } from '../theme';

type Props = { children: React.ReactNode };
type State = { error: string | null };

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error: error?.message || 'خطای ناشناخته' };
  }

  componentDidCatch(error: Error) {
    // Keep a breadcrumb in Metro / logcat
    console.error('App crash:', error);
  }

  render() {
    if (this.state.error) {
      return (
        <View style={styles.wrap}>
          <Text style={styles.title}>اپ بالا نیامد</Text>
          <Text style={styles.body}>{this.state.error}</Text>
          <Pressable
            style={styles.btn}
            onPress={() => this.setState({ error: null })}
          >
            <Text style={styles.btnText}>تلاش دوباره</Text>
          </Pressable>
        </View>
      );
    }
    return this.props.children;
  }
}

const styles = StyleSheet.create({
  wrap: {
    flex: 1,
    backgroundColor: colors.bg,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    gap: 12,
  },
  title: {
    color: colors.text,
    fontFamily: fonts.extraBold,
    fontSize: 18,
    textAlign: 'center',
  },
  body: {
    color: colors.textMute,
    fontFamily: fonts.regular,
    fontSize: 13,
    textAlign: 'center',
    lineHeight: 20,
  },
  btn: {
    marginTop: 8,
    backgroundColor: colors.accent,
    borderRadius: radii.lg,
    paddingHorizontal: 18,
    paddingVertical: 12,
  },
  btnText: {
    color: colors.bg,
    fontFamily: fonts.bold,
    fontSize: 14,
  },
});
