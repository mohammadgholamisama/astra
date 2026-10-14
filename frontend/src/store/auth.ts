import { create } from "zustand";

interface AuthState {
  token: string | null;
  setToken: (t: string | null) => void;
  logout: () => void;
}

export const useAuth = create<AuthState>((set) => ({
  token: localStorage.getItem("astra_token"),
  setToken: (t) => {
    if (t) localStorage.setItem("astra_token", t);
    else localStorage.removeItem("astra_token");
    set({ token: t });
  },
  logout: () => {
    localStorage.removeItem("astra_token");
    set({ token: null });
  },
}));
