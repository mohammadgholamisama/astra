import { create } from "zustand";

export type ThemeMode = "dark" | "light";

function readTheme(): ThemeMode {
  const saved = localStorage.getItem("astra_theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.setAttribute("data-theme", theme);
}

interface ThemeState {
  theme: ThemeMode;
  setTheme: (t: ThemeMode) => void;
  toggle: () => void;
}

const initial = typeof window !== "undefined" ? readTheme() : "dark";
if (typeof window !== "undefined") applyTheme(initial);

export const useTheme = create<ThemeState>((set, get) => ({
  theme: initial,
  setTheme: (theme) => {
    localStorage.setItem("astra_theme", theme);
    applyTheme(theme);
    set({ theme });
  },
  toggle: () => {
    const next: ThemeMode = get().theme === "dark" ? "light" : "dark";
    get().setTheme(next);
  },
}));
