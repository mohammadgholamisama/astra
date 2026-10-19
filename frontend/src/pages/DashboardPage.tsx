import { useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowUpLeft,
  CheckCircle2,
  Clock,
  Download,
  Flame,
  Gauge,
  KeyRound,
  Layers,
  ListOrdered,
  RefreshCw,
  Server,
  Sparkles,
  TrendingUp,
  Users,
  Wifi,
  X,
} from "lucide-react";
import { ApiError, api, SystemStats, TopUserStat, UpdateStatus } from "../lib/api";
import { formatTraffic, toFaDigits, usagePercent } from "../lib/format";

type StatCard = {
  key: string;
  label: string;
  value: ReactNode;
  hint?: string;
  to?: string;
  icon: ReactNode;
  tone?: "default" | "danger" | "accent" | "warn";
};

function usageShare(used: number, maxUsed: number): number {
  if (maxUsed <= 0 || used <= 0) return 0;
  return Math.min(100, Math.round((used / maxUsed) * 100));
}

function TopUserRow({
  user,
  rank,
  maxUsed,
}: {
  user: TopUserStat;
  rank: number;
  maxUsed: number;
}) {
  const pctBar = usageShare(user.used_traffic, maxUsed);
  const limitPct = usagePercent(user.used_traffic, user.data_limit);
  const name = (user.display_name || "").trim() || user.username;

  return (
    <Link
      to={`/users?q=${encodeURIComponent(user.username)}`}
      className="board-top-row"
      style={{ animationDelay: `${rank * 45}ms` }}
      title={`باز کردن ${user.username} در کاربران`}
    >
      <span className={`board-top-rank r${Math.min(rank, 3)}`} aria-hidden>
        {toFaDigits(rank)}
      </span>
      <div className="board-top-meta">
        <div className="board-top-names">
          <strong dir="auto">{name}</strong>
          {name !== user.username ? (
            <span className="board-top-user" dir="ltr">
              {user.username}
            </span>
          ) : null}
        </div>
        <div className="board-top-bar" aria-hidden>
          <span style={{ width: `${pctBar}%` }} />
        </div>
      </div>
      <div className="board-top-stats">
        <em dir="ltr">{formatTraffic(user.used_traffic, { unit: "en" })}</em>
        <small>
          {limitPct != null
            ? `${toFaDigits(limitPct)}٪ از سهم`
            : "بدون سقف"}
        </small>
      </div>
      <span className="board-top-go" aria-hidden>
        <ArrowUpLeft size={14} />
      </span>
    </Link>
  );
}

export default function DashboardPage() {
  const qc = useQueryClient();
  const [updateOpen, setUpdateOpen] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<UpdateStatus | null>(null);
  const [updateErr, setUpdateErr] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["system"],
    queryFn: () => api<SystemStats>("/api/system"),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });

  const checkUpdate = useMutation({
    mutationFn: () => api<UpdateStatus>("/api/system/update"),
    onSuccess: (res) => {
      setUpdateInfo(res);
      if (res.check_ok === false) {
        setUpdateErr(res.message || "بررسی آپدیت ناموفق بود");
      } else {
        setUpdateErr("");
      }
    },
    onError: (e: Error) => {
      setUpdateInfo(null);
      setUpdateErr(e instanceof ApiError ? e.message : e.message);
    },
  });

  const applyUpdate = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; message: string }>("/api/system/update", {
        method: "POST",
      }),
    onSuccess: () => {
      setUpdateErr("");
      window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["system"] });
        void refetch();
        void checkUpdate.mutateAsync().catch(() => undefined);
      }, 4000);
    },
    onError: (e: Error) => {
      setUpdateErr(e instanceof ApiError ? e.message : e.message);
    },
  });

  function openUpdateModal() {
    setUpdateOpen(true);
    setUpdateErr("");
    setUpdateInfo(null);
    checkUpdate.mutate();
  }

  const healthOk = !!data && data.sync_errors === 0 && !(data.users_with_errors || 0);
  const busy = checkUpdate.isPending || applyUpdate.isPending;
  const checkOk = updateInfo?.check_ok !== false;
  const hasUpdate = !!updateInfo?.update_available && checkOk;
  const isLatest =
    !!updateInfo &&
    checkOk &&
    !updateInfo.update_available &&
    !!updateInfo.remote_version;

  const topUsers = data?.top_users || [];
  const maxTopUsed = useMemo(
    () => Math.max(0, ...topUsers.map((u) => Number(u.used_traffic) || 0)),
    [topUsers]
  );

  const activeRatio = data
    ? data.users_total > 0
      ? Math.round((data.users_active / data.users_total) * 100)
      : 0
    : 0;
  const panelRatio = data
    ? data.panels_total > 0
      ? Math.round((data.panels_active / data.panels_total) * 100)
      : 0
    : 0;

  const stats: StatCard[] = data
    ? [
        {
          key: "traffic",
          label: "ترافیک مصرفی",
          value:
            data.total_used_traffic > 0 ? (
              <span dir="ltr">
                {formatTraffic(data.total_used_traffic, { unit: "en" })}
              </span>
            ) : (
              "—"
            ),
          hint: "جمع مصرف همه کاربران",
          icon: <Activity size={18} strokeWidth={2.1} />,
          tone: "accent",
        },
        {
          key: "users",
          label: "کاربران فعال",
          value: (
            <>
              {toFaDigits(data.users_active)}
              <small>/{toFaDigits(data.users_total)}</small>
            </>
          ),
          hint: `${toFaDigits(activeRatio)}٪ از کل کاربران`,
          to: "/users",
          icon: <Users size={18} strokeWidth={2.1} />,
        },
        {
          key: "panels",
          label: "پنل‌های فعال",
          value: (
            <>
              {toFaDigits(data.panels_active)}
              <small>/{toFaDigits(data.panels_total)}</small>
            </>
          ),
          hint: `${toFaDigits(panelRatio)}٪ پنل‌ها آنلاین`,
          to: "/panels",
          icon: <Server size={18} strokeWidth={2.1} />,
        },
        {
          key: "groups",
          label: "گروه‌های فروش",
          value: toFaDigits(data.groups_total),
          hint: "گروه پیکربندی‌شده",
          to: "/groups",
          icon: <Layers size={18} strokeWidth={2.1} />,
        },
        {
          key: "errors",
          label: "کاربر با خطا",
          value: toFaDigits(data.users_with_errors || 0),
          hint:
            (data.users_with_errors || 0) > 0
              ? "برای رفع، فیلتر خطا در کاربران"
              : "بدون خطای پنل فعال",
          to: "/users",
          icon:
            (data.users_with_errors || 0) > 0 ? (
              <AlertTriangle size={18} strokeWidth={2.1} />
            ) : (
              <CheckCircle2 size={18} strokeWidth={2.1} />
            ),
          tone: (data.users_with_errors || 0) > 0 ? "danger" : "default",
        },
        {
          key: "expired",
          label: "منقضی",
          value: toFaDigits(data.users_expired || 0),
          hint: "کاربران منقضی‌شده",
          to: "/users",
          icon: <Clock size={18} strokeWidth={2.1} />,
          tone: (data.users_expired || 0) > 0 ? "warn" : "default",
        },
      ]
    : [];

  return (
    <div className="board board-v2">
      <header className="board-hero board-hero-v2">
        <div className="board-hero-copy">
          <p className="board-kicker">
            <Sparkles size={14} strokeWidth={2.2} aria-hidden />
            نمای زنده سیستم
          </p>
          <h1 className="page-title">داشبورد</h1>
          <p className="board-sub">
            مصرف، سلامت پنل‌ها و پرمصرف‌ترین کاربران را یک‌جا ببینید — با یک کلیک
            مستقیم به همان کاربر در لیست بروید.
          </p>
        </div>
        <div className="board-hero-side">
          {data ? (
            <div
              className={`board-health ${healthOk ? "ok" : "warn"}`}
              role="status"
            >
              <span className={`board-dot ${healthOk ? "ok" : "warn"}`} />
              <div>
                <strong>{healthOk ? "سیستم پایدار" : "نیاز به بررسی"}</strong>
                <span>
                  {toFaDigits(data.panels_active)} پنل ·{" "}
                  {toFaDigits(data.users_active)} کاربر فعال
                  {(data.users_with_errors || 0) > 0
                    ? ` · ${toFaDigits(data.users_with_errors!)} خطا`
                    : ""}
                </span>
              </div>
            </div>
          ) : null}
          <button
            type="button"
            className="btn ghost sm board-upd-open"
            disabled={busy}
            onClick={openUpdateModal}
            title="بررسی آپدیت پنل"
          >
            <Download size={14} />
            بررسی آپدیت
          </button>
        </div>
      </header>

      {isLoading && (
        <div className="board-skel board-skel-v2" aria-hidden>
          <div className="board-skel-metrics">
            <div className="board-skel-block metric" />
            <div className="board-skel-block metric" />
            <div className="board-skel-block metric" />
            <div className="board-skel-block metric" />
            <div className="board-skel-block metric" />
            <div className="board-skel-block metric" />
          </div>
          <div className="board-skel-split">
            <div className="board-skel-block panel" />
            <div className="board-skel-side">
              <div className="board-skel-block pulse" />
              <div className="board-skel-block shortcuts" />
            </div>
          </div>
        </div>
      )}

      {error && <p className="error">{(error as Error).message}</p>}

      {updateOpen &&
        createPortal(
          <div
            className="modal-backdrop board-upd-backdrop"
            onClick={() => !busy && setUpdateOpen(false)}
          >
            <div
              className="modal board-upd-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="board-upd-title"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="modal-head">
                <h2 id="board-upd-title">آپدیت پنل</h2>
                <button
                  type="button"
                  className="modal-close"
                  disabled={busy}
                  onClick={() => setUpdateOpen(false)}
                  aria-label="بستن"
                >
                  <X size={16} />
                </button>
              </div>
              <div className="board-upd-modal-body">
                {checkUpdate.isPending && !updateInfo ? (
                  <div className="board-upd-checking">
                    <RefreshCw size={22} className="spin" aria-hidden />
                    <p>در حال بررسی نسخه جدید…</p>
                  </div>
                ) : updateErr && !updateInfo ? (
                  <p className="error">{updateErr}</p>
                ) : hasUpdate ? (
                  <div className="board-upd-result has-update">
                    <span className="board-upd-badge warn">نسخه جدید</span>
                    <p className="board-upd-lead">
                      نسخه{" "}
                      <strong dir="ltr">
                        v{toFaDigits(updateInfo!.remote_version)}
                      </strong>{" "}
                      آماده نصب است.
                    </p>
                    <p className="muted board-upd-note">
                      نسخه فعلی:{" "}
                      <span dir="ltr">
                        v{toFaDigits(updateInfo!.local_version)}
                      </span>
                      {updateInfo?.message ? ` — ${updateInfo.message}` : ""}
                    </p>
                    {updateErr ? <p className="error">{updateErr}</p> : null}
                  </div>
                ) : isLatest ? (
                  <div className="board-upd-result ok">
                    <CheckCircle2 size={28} aria-hidden />
                    <p className="board-upd-lead">پنل روی آخرین نسخه است</p>
                    <p className="muted board-upd-note" dir="ltr">
                      v
                      {toFaDigits(
                        updateInfo!.local_version || data?.version || ""
                      )}
                    </p>
                  </div>
                ) : (
                  <div className="board-upd-result">
                    <p className="board-upd-lead">نتیجه‌ای دریافت نشد</p>
                    {updateErr ? <p className="error">{updateErr}</p> : null}
                  </div>
                )}
              </div>
              <div className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={busy}
                  onClick={() => {
                    setUpdateErr("");
                    checkUpdate.mutate();
                  }}
                >
                  <RefreshCw
                    size={14}
                    className={checkUpdate.isPending ? "spin" : undefined}
                  />
                  بررسی دوباره
                </button>
                {hasUpdate ? (
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => {
                      if (
                        !window.confirm(
                          "پنل به آخرین نسخه آپدیت شود؟ دیتابیس و تنظیمات پاک نمی‌شوند."
                        )
                      ) {
                        return;
                      }
                      applyUpdate.mutate();
                    }}
                  >
                    <Download size={14} />
                    {applyUpdate.isPending ? "در حال نصب…" : "نصب آپدیت"}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => setUpdateOpen(false)}
                  >
                    بستن
                  </button>
                )}
              </div>
            </div>
          </div>,
          document.body
        )}

      {data && (
        <>
          <section className="board-metrics board-metrics-v2" aria-label="آمار">
            {stats.map((s, i) => {
              const inner = (
                <>
                  <div className="board-metric-top">
                    <span className="board-metric-ico" aria-hidden>
                      {s.icon}
                    </span>
                    {s.to ? (
                      <span className="board-metric-go" aria-hidden>
                        <ArrowUpLeft size={14} />
                      </span>
                    ) : null}
                  </div>
                  <p className="board-metric-label">{s.label}</p>
                  <p className="board-big">{s.value}</p>
                  {s.hint ? <p className="board-metric-hint">{s.hint}</p> : null}
                </>
              );
              const cls = `board-metric${s.tone === "danger" ? " danger" : ""}${
                s.tone === "accent" ? " accent" : ""
              }${s.tone === "warn" ? " warn" : ""}`;
              if (s.to) {
                return (
                  <Link
                    key={s.key}
                    to={s.to}
                    className={cls}
                    style={{ animationDelay: `${i * 40}ms` }}
                  >
                    {inner}
                  </Link>
                );
              }
              return (
                <article
                  key={s.key}
                  className={cls}
                  style={{ animationDelay: `${i * 40}ms` }}
                >
                  {inner}
                </article>
              );
            })}
          </section>

          <div className="board-split">
            <section className="board-top" aria-label="پرمصرف‌ترین کاربران">
              <div className="board-top-head">
                <div>
                  <h2>
                    <Flame size={16} aria-hidden />
                    پرمصرف‌ترین کاربران
                  </h2>
                  <p>بر اساس مصرف فعلی — کلیک کنید تا در کاربران پیدا شود</p>
                </div>
                <Link to="/users" className="board-top-all">
                  همه کاربران
                  <ArrowUpLeft size={14} />
                </Link>
              </div>
              {topUsers.length === 0 ? (
                <div className="board-top-empty">
                  <Gauge size={22} aria-hidden />
                  <p>هنوز مصرفی ثبت نشده</p>
                </div>
              ) : (
                <div className="board-top-list">
                  {topUsers.map((u, i) => (
                    <TopUserRow
                      key={u.username}
                      user={u}
                      rank={i + 1}
                      maxUsed={maxTopUsed}
                    />
                  ))}
                </div>
              )}
            </section>

            <section className="board-side-stack" aria-label="نمای سریع">
              <article className="board-pulse">
                <div className="board-pulse-head">
                  <TrendingUp size={16} aria-hidden />
                  <h2>نبض سیستم</h2>
                </div>
                <div className="board-pulse-grid">
                  <div className="board-pulse-item">
                    <span>نسبت کاربران فعال</span>
                    <strong>{toFaDigits(activeRatio)}٪</strong>
                    <div className="board-pulse-track" aria-hidden>
                      <i style={{ width: `${activeRatio}%` }} />
                    </div>
                  </div>
                  <div className="board-pulse-item">
                    <span>نسبت پنل‌های فعال</span>
                    <strong>{toFaDigits(panelRatio)}٪</strong>
                    <div className="board-pulse-track" aria-hidden>
                      <i style={{ width: `${panelRatio}%` }} />
                    </div>
                  </div>
                  <div className="board-pulse-item">
                    <span>اتمام حجم</span>
                    <strong>{toFaDigits(data.users_limited || 0)}</strong>
                  </div>
                  <div className="board-pulse-item">
                    <span>خطای همگام‌سازی</span>
                    <strong
                      className={data.sync_errors ? "is-danger" : undefined}
                    >
                      {toFaDigits(data.sync_errors)}
                    </strong>
                  </div>
                </div>
              </article>

              <section className="board-shortcuts board-shortcuts-v2">
                <div className="board-shortcuts-head">
                  <h2>دسترسی سریع</h2>
                  <p>میان‌بر به بخش‌های پرکاربرد</p>
                </div>
                <div className="board-quick-row board-quick-v2">
                  <Link to="/users" className="board-chip">
                    <Users size={16} strokeWidth={2.1} />
                    <span>کاربران</span>
                  </Link>
                  <Link to="/panels" className="board-chip">
                    <Server size={16} strokeWidth={2.1} />
                    <span>پنل‌ها</span>
                  </Link>
                  <Link to="/groups" className="board-chip">
                    <Layers size={16} strokeWidth={2.1} />
                    <span>گروه‌ها</span>
                  </Link>
                  <Link to="/config-order" className="board-chip">
                    <ListOrdered size={16} strokeWidth={2.1} />
                    <span>ترتیب کانفیگ</span>
                  </Link>
                  <Link to="/settings" className="board-chip">
                    <Wifi size={16} strokeWidth={2.1} />
                    <span>تنظیمات</span>
                  </Link>
                  <a
                    href="/docs"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="board-chip"
                  >
                    <KeyRound size={16} strokeWidth={2.1} />
                    <span>API Docs</span>
                  </a>
                </div>
              </section>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
