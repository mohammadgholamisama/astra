import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  DatabaseBackup,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Server,
  Settings,
  Users,
  Layers,
} from "lucide-react";
import ThemeToggle from "./ThemeToggle";
import { useAuth } from "../store/auth";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

/** Swagger UI on the panel/API host (same domain in production). */
function docsHref(): string {
  if (API_BASE) return `${API_BASE}/docs`;
  return "/docs";
}

const links = [
  { to: "/", label: "داشبورد", end: true, icon: LayoutDashboard },
  { to: "/panels", label: "پنل‌ها", icon: Server },
  { to: "/groups", label: "گروه‌ها", icon: Layers },
  { to: "/users", label: "کاربران", icon: Users },
  { to: "/api-keys", label: "کلید API", icon: KeyRound },
  { to: "/backup", label: "بک‌آپ", icon: DatabaseBackup },
  { to: "/settings", label: "تنظیمات", icon: Settings },
];

export default function Layout() {
  const logout = useAuth((s) => s.logout);
  const nav = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  const pageLabel = useMemo(() => {
    const hit = links.find((l) =>
      l.end ? location.pathname === l.to : location.pathname.startsWith(l.to)
    );
    return hit?.label ?? "Astra";
  }, [location.pathname]);

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    document.body.style.overflow = menuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [menuOpen]);

  function doLogout() {
    logout();
    nav("/login");
  }

  return (
    <div className={`app-shell${menuOpen ? " menu-open" : ""}`}>
      <header className="topbar">
        <button
          type="button"
          className="menu-toggle"
          aria-label={menuOpen ? "بستن منو" : "باز کردن منو"}
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
        >
          <span />
          <span />
          <span />
        </button>
        <div className="topbar-center">
          <span className="topbar-brand">Astra</span>
          <span className="topbar-page">{pageLabel}</span>
        </div>
        <div className="topbar-actions">
          <ThemeToggle />
          <button type="button" className="btn ghost sm topbar-logout" onClick={doLogout}>
            خروج
          </button>
        </div>
      </header>

      <div
        className="sidebar-backdrop"
        onClick={() => setMenuOpen(false)}
        aria-hidden={!menuOpen}
      />

      <aside className="sidebar">
        <div className="brand">
          <div className="brand-text">
            <span className="brand-name">Astra</span>
            <span className="brand-tag">پنل فروش کانفیگ</span>
          </div>
        </div>

        <nav className="sidebar-nav" aria-label="منوی اصلی">
          {links.map((l) => {
            const Icon = l.icon;
            return (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.end}
                className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
              >
                <span className="nav-icon">
                  <Icon size={18} strokeWidth={2.1} aria-hidden />
                </span>
                <span className="nav-label">{l.label}</span>
              </NavLink>
            );
          })}
        </nav>

        <div className="sidebar-spacer" />

        <div className="sidebar-foot">
          <ThemeToggle variant="switch" className="sidebar-theme" />
          <a
            className="sidebar-docs"
            href={docsHref()}
            target="_blank"
            rel="noopener noreferrer"
          >
            <BookOpen size={17} strokeWidth={2.1} aria-hidden />
            <span>مستندات API</span>
          </a>
          <button type="button" className="sidebar-logout" onClick={doLogout}>
            <LogOut size={17} strokeWidth={2.1} aria-hidden />
            <span>خروج از حساب</span>
          </button>
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
