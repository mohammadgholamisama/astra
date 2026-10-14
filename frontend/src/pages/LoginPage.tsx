import { CSSProperties, FormEvent, useMemo, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import ThemeToggle from "../components/ThemeToggle";
import { login } from "../lib/api";
import { useAuth } from "../store/auth";

type BtnState = "idle" | "loading" | "error" | "success";

export default function LoginPage() {
  const token = useAuth((s) => s.token);
  const setToken = useAuth((s) => s.setToken);
  const nav = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin123");
  const [error, setError] = useState("");
  const [btnState, setBtnState] = useState<BtnState>("idle");

  const orbs = useMemo(
    () =>
      Array.from({ length: 6 }, (_, i) => ({
        id: i,
        style: {
          ["--x" as string]: `${12 + i * 14}%`,
          ["--y" as string]: `${18 + ((i * 17) % 60)}%`,
          ["--s" as string]: `${180 + i * 70}px`,
          ["--d" as string]: `${14 + i * 3}s`,
          ["--delay" as string]: `${i * -2.4}s`,
        } as CSSProperties,
      })),
    []
  );

  const sparks = useMemo(
    () =>
      Array.from({ length: 28 }, (_, i) => ({
        id: i,
        style: {
          left: `${(i * 37) % 100}%`,
          top: `${(i * 53) % 100}%`,
          ["--dur" as string]: `${6 + (i % 7)}s`,
          ["--delay" as string]: `${-i * 0.45}s`,
          ["--size" as string]: `${2 + (i % 3)}px`,
        } as CSSProperties,
      })),
    []
  );

  if (token) return <Navigate to="/" replace />;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBtnState("loading");
    setError("");
    try {
      const res = await login(username, password);
      setBtnState("success");
      await new Promise((r) => setTimeout(r, 480));
      setToken(res.access_token);
      nav("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا در ورود");
      setBtnState("error");
      window.setTimeout(() => setBtnState("idle"), 900);
    }
  }

  const btnLabel =
    btnState === "loading"
      ? "در حال ورود…"
      : btnState === "success"
        ? "وارد شدید"
        : btnState === "error"
          ? "تلاش مجدد"
          : "ورود";

  return (
    <div className="login-page">
      <ThemeToggle className="login-theme" />
      <div className="login-bg" aria-hidden>
        <div className="login-aurora" />
        <div className="login-grid" />
        <div className="login-beam login-beam-a" />
        <div className="login-beam login-beam-b" />
        {orbs.map((o) => (
          <span key={o.id} className="login-orb" style={o.style} />
        ))}
        {sparks.map((s) => (
          <span key={s.id} className="login-spark" style={s.style} />
        ))}
        <div className="login-ring login-ring-a" />
        <div className="login-ring login-ring-b" />
        <div className="login-noise" />
      </div>

      <form className="login-card" onSubmit={onSubmit}>
        <div className="login-brand-wrap">
          <h1 className="login-brand">Astra</h1>
          <p className="login-brand-sub">پنل فروش کانفیگ</p>
        </div>
        <div className="field login-field">
          <label>نام کاربری</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            disabled={btnState === "loading" || btnState === "success"}
          />
        </div>
        <div className="field login-field">
          <label>رمز عبور</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={btnState === "loading" || btnState === "success"}
          />
        </div>
        {error && <div className="error login-error">{error}</div>}
        <button
          className={`btn login-submit is-${btnState}`}
          disabled={btnState === "loading" || btnState === "success"}
        >
          <span className="login-submit-label">{btnLabel}</span>
          {btnState === "loading" && <span className="login-spinner" aria-hidden />}
          {btnState === "success" && <span className="login-check" aria-hidden />}
        </button>
      </form>
    </div>
  );
}
