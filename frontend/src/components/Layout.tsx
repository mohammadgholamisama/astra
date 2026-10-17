import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Cable,
  DatabaseBackup,
  LayoutDashboard,
  LogOut,
  MapPin,
  Server,
  Settings,
  Users,
  Layers,
  X,
  type LucideIcon,
} from "lucide-react";
import ThemeToggle from "./ThemeToggle";
import { useAuth } from "../store/auth";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

/** Swagger UI on the panel/API host (same domain in production). */
function docsHref(): string {
  if (API_BASE) return `${API_BASE}/docs`;
  return "/docs";
}

type NavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
};

type NavSection = {
  id: string;
  title: string;
  links: NavItem[];
};

const navSections: NavSection[] = [
  {
    id: "main",
    title: "مدیریت",
    links: [
      { to: "/", label: "داشبورد", end: true, icon: LayoutDashboard },
      { to: "/panels", label: "پنل‌ها", icon: Server },
      { to: "/groups", label: "گروه‌ها", icon: Layers },
      { to: "/users", label: "کاربران", icon: Users },
    ],
  },
  {
    id: "tools",
    title: "ابزارها",
    links: [
      { to: "/locations", label: "لوکیشن", icon: MapPin },
      { to: "/configs", label: "فیلتر کانفیگ‌ها", icon: Cable },
      { to: "/backup", label: "بک‌آپ", icon: DatabaseBackup },
      { to: "/settings", label: "تنظیمات", icon: Settings },
    ],
  },
];

const allLinks: NavItem[] = navSections.flatMap((s) => s.links);

export default function Layout() {
  const logout = useAuth((s) => s.logout);
  const nav = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  const pageLabel = useMemo(() => {
    const hit = allLinks.find((l) =>
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
          <span className="topbar-page">{pageLabel}</span>
          <span className="topbar-brand">Astra</span>
        </div>
        <div className="topbar-actions">
          <button
            type="button"
            className="topbar-icon-btn"
            onClick={doLogout}
            aria-label="خروج از حساب"
            title="خروج"
          >
            <LogOut size={17} strokeWidth={2.2} />
          </button>
        </div>
      </header>

      <div
        className="sidebar-backdrop"
        onClick={() => setMenuOpen(false)}
        aria-hidden={!menuOpen}
      />

      <aside className="sidebar" aria-label="منوی کناری">
        <div className="sidebar-head">
          <div className="brand">
            <div className="brand-mark" aria-hidden>
              <img src="/astra-vpn.svg" alt="" width={28} height={28} draggable={false} />
            </div>
            <div className="brand-text">
              <span className="brand-name">Astra</span>
              <span className="brand-tag">پنل فروش کانفیگ</span>
            </div>
          </div>
          <button
            type="button"
            className="sidebar-close"
            aria-label="بستن منو"
            onClick={() => setMenuOpen(false)}
          >
            <X size={18} strokeWidth={2.2} />
          </button>
        </div>

        <nav className="sidebar-nav" aria-label="منوی اصلی">
          {navSections.map((section) => (
            <div key={section.id} className="nav-section">
              <p className="nav-section-title">{section.title}</p>
              <div className="nav-section-links">
                {section.links.map((l) => {
                  const Icon = l.icon;
                  return (
                    <NavLink
                      key={l.to}
                      to={l.to}
                      end={l.end}
                      className={({ isActive }) =>
                        `nav-link${isActive ? " active" : ""}`
                      }
                    >
                      <span className="nav-icon">
                        <Icon size={17} strokeWidth={2.1} aria-hidden />
                      </span>
                      <span className="nav-label">{l.label}</span>
                    </NavLink>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <ThemeToggle variant="switch" className="sidebar-theme" />
          <div className="sidebar-foot-row">
            <a
              className="sidebar-foot-link"
              href={docsHref()}
              target="_blank"
              rel="noopener noreferrer"
            >
              <BookOpen size={15} strokeWidth={2.1} aria-hidden />
              <span>مستندات</span>
            </a>
            <button type="button" className="sidebar-foot-link danger" onClick={doLogout}>
              <LogOut size={15} strokeWidth={2.1} aria-hidden />
              <span>خروج</span>
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
