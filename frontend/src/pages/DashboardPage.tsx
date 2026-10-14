import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, SystemStats } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

export default function DashboardPage() {
  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["system"],
    queryFn: () => api<SystemStats>("/api/system"),
    refetchInterval: 30_000,
  });

  const healthOk = !!data && data.sync_errors === 0;

  return (
    <div className="board">
      <header className="board-head">
        <div>
          <h1 className="page-title">داشبورد</h1>
          <p className="board-sub">
            {data
              ? healthOk
                ? "همه‌چیز پایدار است"
                : `${toFaDigits(data.sync_errors)} مورد نیاز به بررسی دارد`
              : "در حال آماده‌سازی"}
          </p>
        </div>
        <button
          type="button"
          className="btn ghost sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          {isFetching ? "…" : "تازه‌سازی"}
        </button>
      </header>

      {isLoading && (
        <div className="board-skel">
          <div className="board-skel-block wide" />
          <div className="board-skel-row">
            <div className="board-skel-block" />
            <div className="board-skel-block" />
            <div className="board-skel-block" />
          </div>
        </div>
      )}

      {error && <p className="error">{(error as Error).message}</p>}

      {data && (
        <>
          <section className="board-status">
            <div className={`board-pulse ${healthOk ? "ok" : "warn"}`} />
            <div className="board-status-text">
              <strong>{healthOk ? "وضعیت سیستم خوب است" : "نیاز به رسیدگی"}</strong>
              <span>
                {toFaDigits(data.panels_active)} پنل فعال · {toFaDigits(data.users_active)} کاربر
                فعال
              </span>
            </div>
            <span className="board-ver">v{toFaDigits(data.version)}</span>
          </section>

          <section className="board-grid">
            <article className="board-card board-traffic">
              <header>
                <span>ترافیک کل</span>
              </header>
              {data.total_used_traffic > 0 ? (
                <p className="board-big">{formatTraffic(data.total_used_traffic)}</p>
              ) : (
                <>
                  <p className="board-big muted-big">—</p>
                  <p className="board-note">هنوز مصرفی ثبت نشده</p>
                </>
              )}
            </article>

            <article className="board-card">
              <header>
                <span>کاربران</span>
                <Link to="/users">مشاهده</Link>
              </header>
              <p className="board-big">
                {toFaDigits(data.users_active)}
                <small>/{toFaDigits(data.users_total)}</small>
              </p>
              <p className="board-note">فعال از کل</p>
            </article>

            <article className="board-card">
              <header>
                <span>پنل‌ها</span>
                <Link to="/panels">مدیریت</Link>
              </header>
              <p className="board-big">
                {toFaDigits(data.panels_active)}
                <small>/{toFaDigits(data.panels_total)}</small>
              </p>
              <p className="board-note">متصل و فعال</p>
            </article>

            <article className="board-card">
              <header>
                <span>گروه‌ها</span>
                <Link to="/groups">مدیریت</Link>
              </header>
              <p className="board-big">{toFaDigits(data.groups_total)}</p>
              <p className="board-note">مسیر فروش چندمنبعی</p>
            </article>

            <article className={`board-card ${data.sync_errors ? "danger" : ""}`}>
              <header>
                <span>همگام‌سازی</span>
              </header>
              <p className="board-big">{toFaDigits(data.sync_errors)}</p>
              <p className="board-note">{data.sync_errors ? "خطای باز" : "بدون خطا"}</p>
            </article>
          </section>

          <section className="board-quick">
            <h2>دسترسی سریع</h2>
            <div className="board-quick-row">
              <Link to="/users" className="board-chip">
                ساخت کاربر
              </Link>
              <Link to="/panels" className="board-chip">
                افزودن پنل
              </Link>
              <Link to="/groups" className="board-chip">
                ساخت گروه
              </Link>
              <Link to="/api-keys" className="board-chip">
                کلید API
              </Link>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
