import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { ApiError, api, SystemStats, UpdateStatus } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

export default function DashboardPage() {
  const qc = useQueryClient();
  const [updateMsg, setUpdateMsg] = useState<{ type: "ok" | "err" | "info"; text: string } | null>(
    null
  );
  const [updateInfo, setUpdateInfo] = useState<UpdateStatus | null>(null);

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ["system"],
    queryFn: () => api<SystemStats>("/api/system"),
    refetchInterval: 30_000,
  });

  const checkUpdate = useMutation({
    mutationFn: () => api<UpdateStatus>("/api/system/update"),
    onSuccess: (res) => {
      setUpdateInfo(res);
      setUpdateMsg({
        type: res.update_available ? "info" : "ok",
        text: res.message,
      });
    },
    onError: (e: Error) => {
      setUpdateMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : e.message,
      });
    },
  });

  const applyUpdate = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; message: string; local_version: string }>("/api/system/update", {
        method: "POST",
      }),
    onSuccess: async (res) => {
      setUpdateMsg({ type: "ok", text: res.message || "آپدیت نصب شد" });
      setUpdateInfo(null);
      // Service restarts — retry stats after a short wait
      window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["system"] });
        refetch();
      }, 4000);
    },
    onError: (e: Error) => {
      setUpdateMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : e.message,
      });
    },
  });

  const healthOk = !!data && data.sync_errors === 0;
  const busy = checkUpdate.isPending || applyUpdate.isPending;

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

          <section className="board-update">
            <div className="board-update-text">
              <strong>بروزرسانی پنل</strong>
              <span>
                {updateInfo
                  ? updateInfo.update_available
                    ? `نسخه ${toFaDigits(updateInfo.remote_version)} آماده نصب است (فعلی: ${toFaDigits(updateInfo.local_version)})`
                    : `نسخه فعلی ${toFaDigits(updateInfo.local_version)} آخرین است`
                  : `نسخه نصب‌شده: ${toFaDigits(data.version)} · دیتابیس و تنظیمات حفظ می‌شوند`}
              </span>
            </div>
            <div className="board-update-actions">
              <button
                type="button"
                className="btn ghost sm"
                disabled={busy}
                onClick={() => {
                  setUpdateMsg(null);
                  checkUpdate.mutate();
                }}
              >
                <RefreshCw size={14} />
                {checkUpdate.isPending ? "در حال بررسی…" : "بررسی آپدیت"}
              </button>
              {updateInfo?.update_available && (
                <button
                  type="button"
                  className="btn sm"
                  disabled={busy}
                  onClick={() => {
                    if (
                      !window.confirm(
                        "پنل به آخرین نسخه آپدیت شود؟ دیتابیس و تنظیمات پاک نمی‌شوند."
                      )
                    ) {
                      return;
                    }
                    setUpdateMsg({
                      type: "info",
                      text: "در حال دانلود و نصب… ممکن است چند دقیقه طول بکشد",
                    });
                    applyUpdate.mutate();
                  }}
                >
                  {applyUpdate.isPending ? "در حال آپدیت…" : "نصب آپدیت"}
                </button>
              )}
            </div>
            {updateMsg && (
              <p className={`admin-toast ${updateMsg.type === "info" ? "ok" : updateMsg.type}`}>
                {updateMsg.text}
              </p>
            )}
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
