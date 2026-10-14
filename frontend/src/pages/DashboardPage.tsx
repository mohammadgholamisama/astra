import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  CheckCircle2,
  Download,
  KeyRound,
  Layers,
  RefreshCw,
  Server,
  Users,
} from "lucide-react";
import { ApiError, api, SystemStats, UpdateStatus } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

export default function DashboardPage() {
  const qc = useQueryClient();
  const [updateInfo, setUpdateInfo] = useState<UpdateStatus | null>(null);
  const [err, setErr] = useState("");

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["system"],
    queryFn: () => api<SystemStats>("/api/system"),
    refetchInterval: 30_000,
  });

  const checkUpdate = useMutation({
    mutationFn: () => api<UpdateStatus>("/api/system/update"),
    onSuccess: (res) => {
      setUpdateInfo(res);
      setErr("");
    },
    onError: (e: Error) => {
      setErr(e instanceof ApiError ? e.message : e.message);
    },
  });

  const applyUpdate = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; message: string }>("/api/system/update", { method: "POST" }),
    onSuccess: () => {
      setUpdateInfo(null);
      setErr("");
      window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["system"] });
        refetch();
      }, 4000);
    },
    onError: (e: Error) => {
      setErr(e instanceof ApiError ? e.message : e.message);
    },
  });

  const healthOk = !!data && data.sync_errors === 0;
  const busy = checkUpdate.isPending || applyUpdate.isPending;
  const hasUpdate = !!updateInfo?.update_available;
  const isLatest = !!updateInfo && !updateInfo.update_available;

  return (
    <div className="board">
      <header className="board-head">
        <h1 className="page-title">داشبورد</h1>
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
          <section className={`board-strip ${healthOk ? "ok" : "warn"}`}>
            <div className="board-strip-status">
              <span className={`board-dot ${healthOk ? "ok" : "warn"}`} />
              <div className="board-strip-text">
                <strong>{healthOk ? "سیستم پایدار" : "نیاز به بررسی"}</strong>
                <span>
                  {toFaDigits(data.panels_active)} پنل · {toFaDigits(data.users_active)} کاربر
                  {!healthOk ? ` · ${toFaDigits(data.sync_errors)} خطا` : ""}
                  <span className="board-ver" dir="ltr">
                    {" "}
                    · v{data.version}
                  </span>
                </span>
              </div>
            </div>

            <div className="board-strip-update">
              {hasUpdate && (
                <span className="board-upd-tag">
                  نسخه {toFaDigits(updateInfo!.remote_version)}
                </span>
              )}
              {isLatest && !busy && (
                <span className="board-upd-ok">
                  <CheckCircle2 size={14} />
                  به‌روز
                </span>
              )}
              {applyUpdate.isPending ? (
                <span className="board-upd-busy">در حال نصب…</span>
              ) : hasUpdate ? (
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
                    applyUpdate.mutate();
                  }}
                >
                  <Download size={14} />
                  نصب آپدیت
                </button>
              ) : (
                <button
                  type="button"
                  className="btn ghost sm"
                  disabled={busy}
                  onClick={() => {
                    setErr("");
                    checkUpdate.mutate();
                  }}
                >
                  <RefreshCw
                    size={14}
                    className={checkUpdate.isPending ? "spin" : undefined}
                  />
                  {checkUpdate.isPending ? "بررسی…" : "بررسی آپدیت"}
                </button>
              )}
            </div>
          </section>

          {err && <p className="error board-upd-err">{err}</p>}

          <section className="board-grid" aria-label="آمار">
            <article className="board-card board-traffic">
              <header>
                <span className="board-card-label">
                  <Activity size={14} />
                  ترافیک
                </span>
              </header>
              <p className={`board-big${data.total_used_traffic ? "" : " muted-big"}`}>
                {data.total_used_traffic > 0 ? formatTraffic(data.total_used_traffic) : "—"}
              </p>
            </article>

            <article className="board-card">
              <header>
                <span className="board-card-label">
                  <Users size={14} />
                  کاربران
                </span>
                <Link to="/users">
                  <ArrowUpRight size={13} />
                </Link>
              </header>
              <p className="board-big">
                {toFaDigits(data.users_active)}
                <small>/{toFaDigits(data.users_total)}</small>
              </p>
            </article>

            <article className="board-card">
              <header>
                <span className="board-card-label">
                  <Server size={14} />
                  پنل‌ها
                </span>
                <Link to="/panels">
                  <ArrowUpRight size={13} />
                </Link>
              </header>
              <p className="board-big">
                {toFaDigits(data.panels_active)}
                <small>/{toFaDigits(data.panels_total)}</small>
              </p>
            </article>

            <article className="board-card">
              <header>
                <span className="board-card-label">
                  <Layers size={14} />
                  گروه‌ها
                </span>
                <Link to="/groups">
                  <ArrowUpRight size={13} />
                </Link>
              </header>
              <p className="board-big">{toFaDigits(data.groups_total)}</p>
            </article>

            <article className={`board-card ${data.sync_errors ? "danger" : ""}`}>
              <header>
                <span className="board-card-label">
                  {data.sync_errors ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
                  همگام‌سازی
                </span>
              </header>
              <p className="board-big">{toFaDigits(data.sync_errors)}</p>
            </article>
          </section>

          <section className="board-quick">
            <div className="board-quick-row">
              <Link to="/users" className="board-chip">
                <Users size={15} />
                کاربر
              </Link>
              <Link to="/panels" className="board-chip">
                <Server size={15} />
                پنل
              </Link>
              <Link to="/groups" className="board-chip">
                <Layers size={15} />
                گروه
              </Link>
              <Link to="/api-keys" className="board-chip">
                <KeyRound size={15} />
                API
              </Link>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
