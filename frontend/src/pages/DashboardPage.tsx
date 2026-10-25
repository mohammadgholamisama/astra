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
  Cpu,
  Download,
  Flame,
  Gauge,
  HardDrive,
  KeyRound,
  Layers,
  ListOrdered,
  MemoryStick,
  Network,
  RefreshCw,
  Server,
  Sparkles,
  TrendingUp,
  Users,
  Wifi,
  X,
} from "lucide-react";
import {
  ApiError,
  api,
  formatBytes,
  HostMetrics,
  SystemStats,
  TopUserStat,
  UpdateStatus,
} from "../lib/api";
import { formatTraffic, toFaDigits, usagePercent } from "../lib/format";

function fmtHostBytes(n: number): string {
  if (!n || n < 0) return "۰ B";
  return toFaDigits(formatBytes(n).replace("∞", "0 B"));
}

function fmtUptime(sec: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const parts: string[] = [];
  if (d) parts.push(`${toFaDigits(d)} روز`);
  if (h || d) parts.push(`${toFaDigits(h)} ساعت`);
  parts.push(`${toFaDigits(m)} دقیقه`);
  return parts.join(" و ");
}

function gaugeTone(pct: number): "ok" | "mid" | "hot" {
  if (pct >= 90) return "hot";
  if (pct >= 70) return "mid";
  return "ok";
}

function serviceTone(state: string): "ok" | "warn" | "bad" | "mute" {
  const s = (state || "").toLowerCase();
  if (s === "active") return "ok";
  if (s === "activating" || s === "deactivating") return "warn";
  if (s === "failed" || s === "inactive") return "bad";
  return "mute";
}

function serviceLabelFa(state: string): string {
  switch ((state || "").toLowerCase()) {
    case "active":
      return "فعال";
    case "inactive":
      return "خاموش";
    case "failed":
      return "خطا";
    case "activating":
      return "در حال روشن";
    case "deactivating":
      return "در حال خاموش";
    default:
      return state || "نامشخص";
  }
}

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

  const {
    data: host,
    isLoading: hostLoading,
    isFetching: hostFetching,
    refetch: refetchHost,
  } = useQuery({
    queryKey: ["system-host"],
    queryFn: () => api<HostMetrics>("/api/system/host"),
    refetchInterval: 5000,
    staleTime: 2000,
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

      {(host || hostLoading) && (
        <section className="board-host" aria-label="وضعیت سرور">
          <div className="board-host-head">
            <div>
              <h2>
                <Server size={16} aria-hidden />
                وضعیت سرور
              </h2>
              <p>
                {host
                  ? `${host.hostname} · ${host.os} · ${host.arch}`
                  : "در حال دریافت…"}
              </p>
            </div>
            <button
              type="button"
              className="btn ghost sm board-refresh"
              disabled={hostFetching}
              onClick={() => void refetchHost()}
              title="تازه‌سازی"
            >
              <RefreshCw
                size={14}
                className={hostFetching ? "spin" : undefined}
              />
              زنده
            </button>
          </div>

          {hostLoading && !host ? (
            <div className="board-host-skel" aria-hidden>
              <div className="board-skel-block metric" />
              <div className="board-skel-block metric" />
              <div className="board-skel-block metric" />
            </div>
          ) : host ? (
            <>
              <div className="board-host-gauges">
                {(
                  [
                    {
                      key: "cpu",
                      label: "CPU",
                      pct: host.cpu_percent,
                      icon: <Cpu size={18} />,
                      detail: `${toFaDigits(host.cpu_count_logical)} هسته${
                        host.cpu_freq_mhz
                          ? ` · ${toFaDigits(Math.round(host.cpu_freq_mhz))} MHz`
                          : ""
                      }`,
                    },
                    {
                      key: "ram",
                      label: "رم",
                      pct: host.memory.percent,
                      icon: <MemoryStick size={18} />,
                      detail: `${fmtHostBytes(host.memory.used)} / ${fmtHostBytes(
                        host.memory.total
                      )}`,
                    },
                    {
                      key: "disk",
                      label: "دیسک",
                      pct: host.disks[0]?.percent ?? 0,
                      icon: <HardDrive size={18} />,
                      detail: host.disks[0]
                        ? `${fmtHostBytes(host.disks[0].used)} / ${fmtHostBytes(
                            host.disks[0].total
                          )}`
                        : "—",
                    },
                  ] as const
                ).map((g) => {
                  const tone = gaugeTone(g.pct);
                  return (
                    <article
                      key={g.key}
                      className={`board-host-gauge tone-${tone}`}
                    >
                      <div className="board-host-gauge-top">
                        <span className="board-host-gauge-ico" aria-hidden>
                          {g.icon}
                        </span>
                        <span>{g.label}</span>
                        <strong dir="ltr">{toFaDigits(g.pct.toFixed(0))}٪</strong>
                      </div>
                      <div className="board-host-gauge-track" aria-hidden>
                        <i style={{ width: `${Math.min(100, g.pct)}%` }} />
                      </div>
                      <p className="board-host-gauge-detail" dir="ltr">
                        {g.detail}
                      </p>
                    </article>
                  );
                })}
              </div>

              <div className="board-host-meta">
                <div>
                  <span>آپتایم</span>
                  <b>{fmtUptime(host.uptime_seconds)}</b>
                </div>
                <div>
                  <span>Load</span>
                  <b dir="ltr">
                    {host.load_avg
                      .map((n) => toFaDigits(Number(n).toFixed(2)))
                      .join(" · ")}
                  </b>
                </div>
                <div>
                  <span>پردازه‌ها</span>
                  <b>{toFaDigits(host.process_count)}</b>
                </div>
                <div>
                  <span>شبکه ↓ / ↑</span>
                  <b dir="ltr">
                    {fmtHostBytes(host.net.bytes_recv)} /{" "}
                    {fmtHostBytes(host.net.bytes_sent)}
                  </b>
                </div>
                {host.swap.total > 0 ? (
                  <div>
                    <span>Swap</span>
                    <b dir="ltr">
                      {toFaDigits(host.swap.percent.toFixed(0))}٪ ·{" "}
                      {fmtHostBytes(host.swap.used)} /{" "}
                      {fmtHostBytes(host.swap.total)}
                    </b>
                  </div>
                ) : null}
                <div>
                  <span>Python</span>
                  <b dir="ltr">{host.python}</b>
                </div>
              </div>

              {host.cpu_per_core.length > 1 ? (
                <div className="board-host-cores" aria-label="هسته‌های CPU">
                  {host.cpu_per_core.map((pct, i) => (
                    <div
                      key={i}
                      className={`board-host-core tone-${gaugeTone(pct)}`}
                      title={`Core ${i}: ${pct}%`}
                    >
                      <span>{toFaDigits(i + 1)}</span>
                      <div className="board-host-core-track" aria-hidden>
                        <i style={{ height: `${Math.min(100, pct)}%` }} />
                      </div>
                      <em dir="ltr">{toFaDigits(Math.round(pct))}٪</em>
                    </div>
                  ))}
                </div>
              ) : null}

              {host.disks.length > 1 ? (
                <div className="board-host-disks">
                  {host.disks.slice(1).map((d) => (
                    <div key={d.mount} className="board-host-disk-row">
                      <HardDrive size={14} aria-hidden />
                      <code dir="ltr">{d.mount}</code>
                      <div className="board-host-gauge-track" aria-hidden>
                        <i
                          className={`tone-${gaugeTone(d.percent)}`}
                          style={{ width: `${Math.min(100, d.percent)}%` }}
                        />
                      </div>
                      <span dir="ltr">
                        {toFaDigits(d.percent.toFixed(0))}٪ ·{" "}
                        {fmtHostBytes(d.used)} / {fmtHostBytes(d.total)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="board-host-services">
                <div className="board-host-services-head">
                  <Network size={14} aria-hidden />
                  <h3>سرویس‌ها</h3>
                </div>
                <div className="board-host-svc-grid">
                  {host.services.map((s) => {
                    const tone = serviceTone(s.state);
                    return (
                      <div
                        key={s.name}
                        className={`board-host-svc tone-${tone}`}
                        title={s.name}
                      >
                        <span className={`board-host-svc-dot tone-${tone}`} />
                        <div>
                          <strong>{s.label}</strong>
                          <small>{serviceLabelFa(s.state)}</small>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {host.note ? (
                <p className="board-host-note muted">{host.note}</p>
              ) : null}
            </>
          ) : null}
        </section>
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
