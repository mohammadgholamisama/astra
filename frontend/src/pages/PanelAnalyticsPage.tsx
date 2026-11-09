import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  BookOpen,
  Gauge,
  Loader2,
  Minus,
  Radio,
  RefreshCw,
  Server,
  Smartphone,
  Users,
  Wifi,
  Zap,
} from "lucide-react";
import AnalyticsGuideModal, {
  PANEL_ANALYTICS_GUIDE,
} from "../components/AnalyticsGuide";
import { api } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

type PanelRow = {
  panel_id: number;
  name: string;
  type: string;
  is_active: boolean;
  accounts?: number;
  online: number;
  raw: {
    current_total?: number;
    hour: number;
    day: number;
    week: number;
    month: number;
    lifetime_cursor: number;
  };
  compare_hour_pct: number | null;
  rate: {
    hour_bps: number;
    hour_label: string;
    peak_bps: number;
    peak_label: string;
    per_online_bytes_hour: number | null;
    per_online_label: string | null;
  };
  tests: {
    runs: number;
    configs_tested: number;
    top100: number;
    good: number;
    all_ops_ok: number;
    last_test_at?: string | null;
    last_run?: {
      tested: number;
      in_top100: number;
      good: number;
      all_ops_ok: number;
      avg_ping: number;
      ts?: string | null;
    } | null;
  };
};

type Overview = {
  generated_at: string;
  online: {
    total_online: number;
    panel_online_total: number;
    operators: Record<string, { count: number; label: string }>;
    brands: Record<string, number>;
    by_panel: {
      panel_id: number;
      online: number;
      operators: Record<string, { count: number; label: string }>;
      brands: Record<string, number>;
    }[];
  };
  totals: {
    raw_current?: number;
    raw_hour: number;
    raw_day: number;
    panels: number;
  };
  global_hourly: { hour_ts: number; raw_bytes: number }[];
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  panels: PanelRow[];
  note?: string;
};

type Detail = {
  panel_id: number;
  name: string;
  type: string;
  accounts: number;
  online: {
    online: number;
    operators: Record<string, { count: number; label: string }>;
    brands: Record<string, number>;
  };
  peak_bps_label: string;
  lifetime_raw: number;
  current_total?: number;
  compare: Record<
    string,
    { current: number; previous: number; change_pct: number | null }
  >;
  hourly: {
    hour_ts: number;
    raw_bytes: number;
    avg_mbytes_s: number;
    avg_online: number;
    online_peak: number;
    per_online_label: string;
  }[];
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  tests: {
    runs: number;
    configs_tested: number;
    top100: number;
    good: number;
    all_ops_ok: number;
    history: {
      ts?: string | null;
      tested: number;
      in_top100: number;
      good: number;
      all_ops_ok: number;
      avg_ping: number;
    }[];
  };
  rate_now: { hour_label: string; per_online_bps: number };
};

function ChangePill({ pct }: { pct: number | null | undefined }) {
  if (pct == null || Number.isNaN(pct)) {
    return <span className="an-change muted">—</span>;
  }
  const up = pct > 0;
  const flat = Math.abs(pct) < 0.05;
  const Icon = flat ? Minus : up ? ArrowUpRight : ArrowDownRight;
  const cls = flat ? "muted" : up ? "up" : "down";
  return (
    <span className={`an-change ${cls}`}>
      <Icon size={14} />
      {toFaDigits(Math.abs(pct).toFixed(1))}٪
    </span>
  );
}

function BarRow({
  label,
  value,
  max,
  sub,
}: {
  label: string;
  value: number;
  max: number;
  sub?: string;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="an-bar-row">
      <div className="an-bar-meta">
        <span>{label}</span>
        <b>{formatTraffic(value)}</b>
      </div>
      <div className="an-bar-track">
        <i style={{ width: `${pct}%` }} />
      </div>
      {sub ? <small className="muted">{sub}</small> : null}
    </div>
  );
}

function hourLabel(ts: number) {
  return new Date(ts * 1000).toLocaleString("fa-IR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
  });
}

function DistChips({
  items,
}: {
  items: { label: string; count: number }[];
}) {
  if (!items.length) return <p className="muted">هنوز داده‌ای نیست</p>;
  const max = Math.max(1, ...items.map((x) => x.count));
  return (
    <div className="pan-dist">
      {items.map((x) => (
        <div key={x.label} className="pan-dist-row">
          <div className="pan-dist-meta">
            <span>{x.label}</span>
            <b>{toFaDigits(x.count)}</b>
          </div>
          <div className="an-bar-track">
            <i style={{ width: `${(x.count / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function PanelAnalyticsPage() {
  const qc = useQueryClient();
  const [focusId, setFocusId] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [sortKey, setSortKey] = useState<"total" | "hour" | "online" | "day">(
    "total"
  );
  const [guideOpen, setGuideOpen] = useState(false);

  const overview = useQuery({
    queryKey: ["analytics-panels"],
    queryFn: () => api<Overview>("/api/analytics/panels"),
    refetchInterval: 60_000,
  });

  const detail = useQuery({
    queryKey: ["analytics-panel", focusId],
    queryFn: () => api<Detail>(`/api/analytics/panels/${focusId}`),
    enabled: focusId != null,
    refetchInterval: focusId != null ? 45_000 : false,
  });

  const liveRefresh = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; stats: Record<string, number>; overview: Overview }>(
        "/api/analytics/panels/refresh",
        { method: "POST", timeoutMs: 180_000 }
      ),
    onSuccess: (res) => {
      if (res.overview) {
        qc.setQueryData(["analytics-panels"], res.overview);
      } else {
        void qc.invalidateQueries({ queryKey: ["analytics-panels"] });
      }
      if (focusId != null) {
        void qc.invalidateQueries({ queryKey: ["analytics-panel", focusId] });
      }
    },
  });

  const data = overview.data;
  const maxHour = useMemo(() => {
    if (!data?.global_hourly?.length) return 1;
    return Math.max(1, ...data.global_hourly.map((h) => h.raw_bytes));
  }, [data]);
  const maxPanelTotal = useMemo(() => {
    if (!data?.panels?.length) return 1;
    return Math.max(
      1,
      ...data.panels.map((p) => Number(p.raw.current_total || p.raw.hour || 0))
    );
  }, [data]);

  const opItems = useMemo(() => {
    if (!data?.online?.operators) return [];
    return Object.entries(data.online.operators)
      .map(([code, v]) => ({ label: v.label || code, count: v.count }))
      .sort((a, b) => b.count - a.count);
  }, [data]);

  const brandItems = useMemo(() => {
    if (!data?.online?.brands) return [];
    return Object.entries(data.online.brands)
      .map(([label, count]) => ({ label, count }))
      .sort((a, b) => b.count - a.count);
  }, [data]);

  const panelsView = useMemo(() => {
    let list = [...(data?.panels || [])];
    const needle = q.trim().toLowerCase();
    if (needle) {
      list = list.filter(
        (p) =>
          p.name.toLowerCase().includes(needle) ||
          p.type.toLowerCase().includes(needle)
      );
    }
    list.sort((a, b) => {
      if (sortKey === "online") return b.online - a.online;
      if (sortKey === "day") return b.raw.day - a.raw.day;
      if (sortKey === "hour") return b.raw.hour - a.raw.hour;
      return (
        Number(b.raw.current_total || 0) - Number(a.raw.current_total || 0)
      );
    });
    return list;
  }, [data, q, sortKey]);

  const d = detail.data;
  const refreshing = liveRefresh.isPending;

  return (
    <div className="an-page">
      <header className="an-hero">
        <div>
          <p className="an-kicker">تحلیل زیر‌ساخت</p>
          <h1>آمار پنل‌ها</h1>
          <p className="an-sub">
            مصرف خام هر پنل، آنلاین‌ها، اپراتور و دستگاه، کیفیت تست پینگ، و نرخ مصرف
            نسبت به تعداد آنلاین
          </p>
          <div className="an-hero-actions">
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => setGuideOpen(true)}
            >
              <BookOpen size={14} />
              راهنما
            </button>
            <Link to="/analytics" className="btn ghost sm">
              <Users size={14} />
              آمار کاربران
            </Link>
            <button
              type="button"
              className="btn sm"
              disabled={refreshing}
              onClick={() => liveRefresh.mutate()}
              title="مصرف زنده از پنل‌ها خوانده و آمار از نو ساخته می‌شود"
            >
              {refreshing ? (
                <Loader2 size={14} className="configs-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              {refreshing ? "در حال خواندن زنده…" : "تازه‌سازی زنده"}
            </button>
            {data?.generated_at ? (
              <span className="an-updated">
                آخرین نمونه:{" "}
                {new Date(data.generated_at).toLocaleTimeString("fa-IR")}
              </span>
            ) : null}
          </div>
          {liveRefresh.isError ? (
            <p className="an-sub" style={{ color: "var(--danger)" }}>
              تازه‌سازی ناموفق بود — دوباره تلاش کنید
            </p>
          ) : null}
        </div>
      </header>

      <AnalyticsGuideModal
        open={guideOpen}
        onClose={() => setGuideOpen(false)}
        title="راهنمای آمار پنل‌ها"
        intro="مصرف‌ها خام و بدون ضریب‌اند. تست پینگ از ping-hub می‌آید."
        items={PANEL_ANALYTICS_GUIDE}
      />

      {focusId != null ? (
        <section className="an-user-panel">
          <div className="an-user-head">
            <button type="button" className="btn ghost sm" onClick={() => setFocusId(null)}>
              ← بازگشت
            </button>
            <div>
              <h2>{d?.name || `پنل #${focusId}`}</h2>
              {d ? (
                <p className="muted">
                  {d.type} · {toFaDigits(d.accounts)} اکانت ·{" "}
                  {toFaDigits(d.online.online)} آنلاین
                </p>
              ) : null}
            </div>
          </div>

          <div className="an-hero-actions" style={{ marginBottom: 12 }}>
            <button
              type="button"
              className="btn sm"
              disabled={refreshing}
              onClick={() => liveRefresh.mutate()}
            >
              {refreshing ? (
                <Loader2 size={14} className="configs-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              تازه‌سازی زنده
            </button>
          </div>

          {detail.isLoading ? (
            <div className="admin-skel">
              <div className="admin-skel-card" />
            </div>
          ) : d ? (
            <>
              <div className="an-stat-grid">
                <article className="an-stat" title="جمع خام اکانت‌های همین پنل">
                  <span>
                    <Zap size={13} /> مصرف فعلی
                  </span>
                  <strong>
                    {formatTraffic(Number(d.current_total ?? d.lifetime_raw ?? 0))}
                  </strong>
                  <small>همیشه از اکانت‌ها</small>
                </article>
                <article className="an-stat">
                  <span>
                    <Activity size={13} /> این ساعت
                  </span>
                  <strong>{formatTraffic(d.compare.hour.current)}</strong>
                  <small>
                    <ChangePill pct={d.compare.hour.change_pct} />
                  </small>
                </article>
                <article className="an-stat">
                  <span>
                    <Radio size={13} /> آنلاین
                  </span>
                  <strong>{toFaDigits(d.online.online)}</strong>
                  <small dir="ltr">نرخ {d.rate_now.hour_label}</small>
                </article>
                <article className="an-stat">
                  <span>
                    <Gauge size={13} /> تست‌ها
                  </span>
                  <strong>{toFaDigits(d.tests.runs)}</strong>
                  <small>
                    top100 {toFaDigits(d.tests.top100)} · خوب{" "}
                    {toFaDigits(d.tests.good)}
                  </small>
                </article>
              </div>

              <div className="an-grid-2">
                <section className="an-block">
                  <h3>مقایسه مصرف خام</h3>
                  {(["hour", "day", "week", "month"] as const).map((k) => {
                    const row = d.compare[k];
                    const title =
                      k === "hour"
                        ? "ساعت"
                        : k === "day"
                          ? "روز"
                          : k === "week"
                            ? "هفته"
                            : "ماه";
                    return (
                      <div key={k} className="an-compare-row">
                        <div>
                          <b>{title}</b>
                          <p>
                            {formatTraffic(row.current)}{" "}
                            <span className="muted">vs</span>{" "}
                            {formatTraffic(row.previous)}
                          </p>
                        </div>
                        <ChangePill pct={row.change_pct} />
                      </div>
                    );
                  })}
                </section>
                <section className="an-block">
                  <h3>
                    <Wifi size={16} /> اپراتور آنلاین‌ها
                  </h3>
                  <DistChips
                    items={Object.entries(d.online.operators || {}).map(
                      ([code, v]) => ({
                        label: v.label || code,
                        count: v.count,
                      })
                    )}
                  />
                  <h3 style={{ marginTop: 18 }}>
                    <Smartphone size={16} /> دستگاه
                  </h3>
                  <DistChips
                    items={Object.entries(d.online.brands || {}).map(
                      ([label, count]) => ({ label, count })
                    )}
                  />
                </section>
              </div>

              <section className="an-block">
                <h3>۴۸ ساعت اخیر (خام · بدون ضریب)</h3>
                <div className="an-bars">
                  {d.hourly.map((h) => (
                    <BarRow
                      key={h.hour_ts}
                      label={hourLabel(h.hour_ts)}
                      value={h.raw_bytes}
                      max={Math.max(1, ...d.hourly.map((x) => x.raw_bytes))}
                      sub={
                        h.raw_bytes > 0
                          ? `≈ ${h.avg_mbytes_s.toFixed(3)} MB/s · آنلاین≈${toFaDigits(h.avg_online)} · سرانه ${h.per_online_label}`
                          : undefined
                      }
                    />
                  ))}
                </div>
              </section>

              <section className="an-block">
                <h3>بازهٔ مصرف در روز</h3>
                <div className="an-hod">
                  {d.hour_of_day.bytes.map((b, i) => {
                    const m = Math.max(1, ...d.hour_of_day.bytes);
                    const pct = (b / m) * 100;
                    const peak = d.hour_of_day.peak_hour === i;
                    return (
                      <div
                        key={i}
                        className={`an-hod-col${peak ? " peak" : ""}`}
                        title={`${toFaDigits(i)}:۰۰ — ${formatTraffic(b)}`}
                      >
                        <i style={{ height: `${Math.max(4, pct)}%` }} />
                        <span>{toFaDigits(i)}</span>
                      </div>
                    );
                  })}
                </div>
                {d.hour_of_day.peak_hour != null ? (
                  <p className="muted">
                    اوج حدود ساعت {toFaDigits(d.hour_of_day.peak_hour)} (
                    {formatTraffic(d.hour_of_day.peak_bytes)})
                  </p>
                ) : null}
              </section>

              <section className="an-block">
                <h3>تاریخچه تست پینگ</h3>
                {d.tests.history.length === 0 ? (
                  <p className="muted">
                    هنوز تست پینگی به این پنل نسبت داده نشده — بعد از همگام‌سازی
                    ping-hub پر می‌شود
                  </p>
                ) : (
                  <div className="an-table-wrap">
                    <table className="an-table">
                      <thead>
                        <tr>
                          <th>زمان</th>
                          <th>تست‌شده</th>
                          <th>جزو ۱۰۰</th>
                          <th>خوب</th>
                          <th>همه اپراتور</th>
                          <th>پینگ میانگین</th>
                        </tr>
                      </thead>
                      <tbody>
                        {d.tests.history.map((t, i) => (
                          <tr key={i}>
                            <td>
                              {t.ts
                                ? new Date(t.ts).toLocaleString("fa-IR")
                                : "—"}
                            </td>
                            <td>{toFaDigits(t.tested)}</td>
                            <td>{toFaDigits(t.in_top100)}</td>
                            <td>{toFaDigits(t.good)}</td>
                            <td>{toFaDigits(t.all_ops_ok)}</td>
                            <td dir="ltr">
                              {t.avg_ping > 0
                                ? `${t.avg_ping.toFixed(0)} ms`
                                : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          ) : (
            <div className="admin-empty">
              <p>خطا در بارگذاری جزئیات پنل</p>
            </div>
          )}
        </section>
      ) : overview.isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : data ? (
        <>
          {refreshing ? (
            <div className="an-fresh-loading">
              <Loader2 size={22} className="configs-spin" />
              <div>
                <b>در حال خواندن مصرف زنده از پنل‌ها…</b>
                <p>بعد از اتمام، جدول و نمودارها با دیتای تازه پر می‌شوند</p>
              </div>
            </div>
          ) : null}

          <div className="an-stat-grid">
            <article className="an-stat">
              <span>
                <Radio size={13} /> آنلاین کل
              </span>
              <strong>{toFaDigits(data.online.total_online)}</strong>
              <small>
                اسلات پنل: {toFaDigits(data.online.panel_online_total)}
              </small>
            </article>
            <article className="an-stat" title="جمع خام همه اکانت‌ها روی همه پنل‌ها">
              <span>
                <Zap size={13} /> مصرف فعلی کل
              </span>
              <strong>
                {formatTraffic(Number(data.totals.raw_current || 0))}
              </strong>
              <small>همیشه از اکانت‌ها — دقیق</small>
            </article>
            <article className="an-stat">
              <span>
                <Activity size={13} /> این ساعت / امروز
              </span>
              <strong>{formatTraffic(data.totals.raw_hour)}</strong>
              <small>امروز: {formatTraffic(data.totals.raw_day)}</small>
            </article>
            <article className="an-stat">
              <span>
                <Server size={13} /> پنل‌ها
              </span>
              <strong>{toFaDigits(data.totals.panels)}</strong>
              <small>
                اوج روز:{" "}
                {data.hour_of_day.peak_hour != null
                  ? `ساعت ${toFaDigits(data.hour_of_day.peak_hour)}`
                  : "—"}
              </small>
            </article>
          </div>

          <div className="an-grid-2">
            <section className="an-block">
              <div className="an-block-head">
                <h3>
                  <Wifi size={16} /> آنلاین بر اساس اپراتور
                </h3>
                <span className="muted">از IP ساب · تقریبی</span>
              </div>
              <DistChips items={opItems} />
            </section>
            <section className="an-block">
              <div className="an-block-head">
                <h3>
                  <Smartphone size={16} /> آنلاین بر اساس دستگاه
                </h3>
                <span className="muted">از User-Agent</span>
              </div>
              <DistChips items={brandItems} />
            </section>
          </div>

          <div className="an-grid-2">
            <section className="an-block">
              <h3>مصرف کل پنل‌ها — ۲۴ ساعت</h3>
              <div className="an-bars">
                {data.global_hourly.map((h) => (
                  <BarRow
                    key={h.hour_ts}
                    label={hourLabel(h.hour_ts)}
                    value={h.raw_bytes}
                    max={maxHour}
                  />
                ))}
              </div>
            </section>
            <section className="an-block">
              <div className="an-block-head">
                <h3>
                  <Gauge size={16} /> سهم مصرف فعلی پنل‌ها
                </h3>
                <span className="muted">کلیک = جزئیات</span>
              </div>
              <div className="an-bars">
                {panelsView
                  .filter((p) => Number(p.raw.current_total || 0) > 0 || p.raw.hour > 0)
                  .map((p) => (
                  <button
                    key={p.panel_id}
                    type="button"
                    className="pan-bar-btn"
                    onClick={() => setFocusId(p.panel_id)}
                    title={`${p.name} — مصرف فعلی ${formatTraffic(Number(p.raw.current_total || 0))} · این ساعت ${formatTraffic(p.raw.hour)}`}
                  >
                    <BarRow
                      label={`${p.name} · ${toFaDigits(p.online)} آنلاین`}
                      value={Number(p.raw.current_total || p.raw.hour || 0)}
                      max={maxPanelTotal}
                      sub={`فعلی ${formatTraffic(Number(p.raw.current_total || 0))} · ساعت ${formatTraffic(p.raw.hour)}`}
                    />
                  </button>
                ))}
                {panelsView.every(
                  (p) => Number(p.raw.current_total || 0) <= 0 && p.raw.hour <= 0
                ) ? (
                  <p className="muted">
                    هنوز مصرفی نیست — «تازه‌سازی زنده» را بزنید
                  </p>
                ) : null}
              </div>
            </section>
          </div>

          <section className="an-block">
            <div className="an-block-head">
              <h3>جدول پنل‌ها</h3>
              <span className="muted">{toFaDigits(panelsView.length)} پنل</span>
            </div>
            <div className="an-toolbar">
              <input
                className="an-filter-input"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="جستجوی نام پنل…"
              />
              <div className="an-sort-pills">
                {(
                  [
                    ["total", "مصرف فعلی"],
                    ["hour", "ساعت"],
                    ["day", "امروز"],
                    ["online", "آنلاین"],
                  ] as const
                ).map(([k, label]) => (
                  <button
                    key={k}
                    type="button"
                    className={`an-sort-pill${sortKey === k ? " is-active" : ""}`}
                    onClick={() => setSortKey(k)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="an-table-wrap">
              <table className="an-table">
                <thead>
                  <tr>
                    <th>پنل</th>
                    <th title="آنلاین روی همین پنل">آنلاین</th>
                    <th title="جمع خام فعلی همه اکانت‌های این پنل — همیشه دقیق">
                      مصرف فعلی
                    </th>
                    <th title="دلتای نمونه‌برداری این ساعت">ساعت</th>
                    <th>امروز</th>
                    <th>هفته</th>
                    <th>ماه</th>
                    <th title="نرخ تقریبی مصرف">نرخ</th>
                    <th title="تعداد دورهای تست ثبت‌شده">تست</th>
                    <th title="آخرین دور / جمع عمری در ۱۰۰تای برتر">top100</th>
                    <th title="آخرین دور / جمع عمری کیفیت خوب">خوب</th>
                    <th title="آخرین دور / جمع عمری OK روی همه اپراتور">همه اپ</th>
                  </tr>
                </thead>
                <tbody>
                  {panelsView.map((p) => (
                    <tr key={p.panel_id}>
                      <td>
                        <button
                          type="button"
                          className="an-linkish"
                          onClick={() => setFocusId(p.panel_id)}
                        >
                          <b>{p.name}</b>
                        </button>
                        <div className="muted" style={{ fontSize: "0.75rem" }}>
                          {p.type}
                          {p.accounts != null
                            ? ` · ${toFaDigits(p.accounts)} اکانت`
                            : ""}
                          {!p.is_active ? " · خاموش" : ""}
                        </div>
                      </td>
                      <td>{toFaDigits(p.online)}</td>
                      <td>
                        <b>{formatTraffic(Number(p.raw.current_total || 0))}</b>
                      </td>
                      <td>
                        {formatTraffic(p.raw.hour)}{" "}
                        <ChangePill pct={p.compare_hour_pct} />
                      </td>
                      <td>{formatTraffic(p.raw.day)}</td>
                      <td>{formatTraffic(p.raw.week)}</td>
                      <td>{formatTraffic(p.raw.month)}</td>
                      <td dir="ltr">{p.rate.hour_label}</td>
                      <td>{toFaDigits(p.tests.runs)}</td>
                      <td>
                        {toFaDigits(p.tests.last_run?.in_top100 ?? 0)}
                        <span className="muted">
                          {" "}
                          / {toFaDigits(p.tests.top100)}
                        </span>
                      </td>
                      <td>
                        {toFaDigits(p.tests.last_run?.good ?? 0)}
                        <span className="muted">
                          {" "}
                          / {toFaDigits(p.tests.good)}
                        </span>
                      </td>
                      <td>
                        {toFaDigits(p.tests.last_run?.all_ops_ok ?? 0)}
                        <span className="muted">
                          {" "}
                          / {toFaDigits(p.tests.all_ops_ok)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {panelsView.length === 0 ? (
              <p className="muted" style={{ marginTop: 10 }}>
                پنلی با این جستجو پیدا نشد
              </p>
            ) : null}
            <p className="muted" style={{ marginTop: 10 }}>
              {data.note} · ستون‌های تست: آخرین دور / جمع عمری · برای جزئیات روی نام
              پنل کلیک کنید
            </p>
          </section>
        </>
      ) : (
        <div className="admin-empty">
          <p>خطا در بارگذاری آمار پنل‌ها</p>
        </div>
      )}
    </div>
  );
}
