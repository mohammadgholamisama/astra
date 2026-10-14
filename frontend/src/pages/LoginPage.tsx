import { FormEvent, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import ThemeToggle from "../components/ThemeToggle";
import { login } from "../lib/api";
import { useAuth } from "../store/auth";

type BtnState = "idle" | "loading" | "error" | "success";

export default function LoginPage() {
  const token = useAuth((s) => s.token);
  const setToken = useAuth((s) => s.setToken);
  const nav = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [btnState, setBtnState] = useState<BtnState>("idle");

  if (token) return <Navigate to="/" replace />;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBtnState("loading");
    setError("");
    try {
      const res = await login(username, password);
      setBtnState("success");
      setToken(res.access_token);
      nav("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا در ورود");
      setBtnState("error");
      window.setTimeout(() => setBtnState("idle"), 700);
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
      <form className="login-card" onSubmit={onSubmit}>
        <div className="login-brand-wrap">
          <h1 className="login-brand">Astra</h1>
          <p className="login-brand-sub">پنل فروش کانفیگ</p>
        </div>
        <div className="field">
          <label>نام کاربری</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            placeholder="نام کاربری"
            disabled={btnState === "loading" || btnState === "success"}
          />
        </div>
        <div className="field">
          <label>رمز عبور</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            placeholder="رمز عبور"
            disabled={btnState === "loading" || btnState === "success"}
          />
        </div>
        {error && <div className="error login-error">{error}</div>}
        <button
          type="submit"
          className={`btn login-submit is-${btnState}`}
          disabled={btnState === "loading" || btnState === "success"}
        >
          {btnLabel}
        </button>
      </form>
    </div>
  );
}
