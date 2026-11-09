import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Gauge,
  Minus,
  Radio,
  Server,
  Smartphone,
  Wifi,
  Zap,
} from "lucide-react";
import { api } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

type PanelRow = {
  panel_id: number;
  name: string;
  type: string;
  is_active: boolean;
  online: number;
  raw: {
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
  totals: { raw_hour: number; raw_day: number; panels: number };
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
  const [focusId, setFocusId] = useState<number | null>(null);

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

  const data = overview.data;
  const maxHour = useMemo(() => {
    if (!data?.global_hourly?.length) return 1;
    return Math.max(1, ...data.global_hourly.map((h) => h.raw_bytes));
  }, [data]);
  const maxPanelHour = useMemo(() => {
    if (!data?.panels?.length) return 1;
    return Math.max(1, ...data.panels.map((p) => p.raw.hour));
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

  const d = detail.data;

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
        </div>
      </header>

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

          {detail.isLoading ? (
            <div className="admin-skel">
              <div className="admin-skel-card" />
            </div>
          ) : d ? (
            <>
              <div className="an-cards">
                <article className="an-card">
                  <p className="an-card-label">مصرف این ساعت</p>
                  <p className="an-card-big">{formatTraffic(d.compare.hour.current)}</p>
                  <ChangePill pct={d.compare.hour.change_pct} />
                </article>
                <article className="an-card">
                  <p className="an-card-label">نرخ تقریبی</p>
                  <p className="an-card-big" dir="ltr">
                    {d.rate_now.hour_label}
                  </p>
                  <p className="an-card-hint">پیک عمری: {d.peak_bps_label}</p>
                </article>
                <article className="an-card">
                  <p className="an-card-label">آنلاین این پنل</p>
                  <p className="an-card-big">{toFaDigits(d.online.online)}</p>
                </article>
                <article className="an-card">
                  <p className="an-card-label">تست‌های ثبت‌شده</p>
                  <p className="an-card-big">{toFaDigits(d.tests.runs)}</p>
                  <p className="an-card-hint">
                    top100: {toFaDigits(d.tests.top100)} · خوب:{" "}
                    {toFaDigits(d.tests.good)} · همه اپراتور:{" "}
                    {toFaDigits(d.tests.all_ops_ok)}
                  </p>
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
          <div className="an-cards">
            <article className="an-card">
              <p className="an-card-label">
                <Radio size={14} /> آنلاین کل
              </p>
              <p className="an-card-big">
                {toFaDigits(data.online.total_online)}
              </p>
              <p className="an-card-hint">
                جمع اسلات پنل: {toFaDigits(data.online.panel_online_total)}
              </p>
            </article>
            <article className="an-card">
              <p className="an-card-label">
                <Zap size={14} /> مصرف خام این ساعت
              </p>
              <p className="an-card-big">{formatTraffic(data.totals.raw_hour)}</p>
            </article>
            <article className="an-card">
              <p className="an-card-label">
                <Activity size={14} /> مصرف امروز
              </p>
              <p className="an-card-big">{formatTraffic(data.totals.raw_day)}</p>
            </article>
            <article className="an-card">
              <p className="an-card-label">
                <Server size={14} /> پنل‌ها
              </p>
              <p className="an-card-big">{toFaDigits(data.totals.panels)}</p>
              <p className="an-card-hint">
                اوج روز:{" "}
                {data.hour_of_day.peak_hour != null
                  ? `ساعت ${toFaDigits(data.hour_of_day.peak_hour)}`
                  : "—"}
              </p>
            </article>
          </div>

          <div className="an-grid-2">
            <section className="an-block">
              <h3>
                <Wifi size={16} /> آنلاین بر اساس اپراتور
              </h3>
              <DistChips items={opItems} />
            </section>
            <section className="an-block">
              <h3>
                <Smartphone size={16} /> آنلاین بر اساس دستگاه
              </h3>
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
              <h3>
                <Gauge size={16} /> سهم مصرف این ساعت
              </h3>
              <div className="an-bars">
                {data.panels.map((p) => (
                  <button
                    key={p.panel_id}
                    type="button"
                    className="pan-bar-btn"
                    onClick={() => setFocusId(p.panel_id)}
                  >
                    <BarRow
                      label={`${p.name} · ${toFaDigits(p.online)} آنلاین`}
                      value={p.raw.hour}
                      max={maxPanelHour}
                      sub={`${p.rate.hour_label}${
                        p.rate.per_online_label
                          ? ` · سرانه ${p.rate.per_online_label}`
                          : ""
                      }`}
                    />
                  </button>
                ))}
              </div>
            </section>
          </div>

          <section className="an-block">
            <h3>جدول پنل‌ها</h3>
            <div className="an-table-wrap">
              <table className="an-table">
                <thead>
                  <tr>
                    <th>پنل</th>
                    <th>آنلاین</th>
                    <th>ساعت</th>
                    <th>امروز</th>
                    <th>هفته</th>
                    <th>ماه</th>
                    <th>نرخ</th>
                    <th>تست</th>
                    <th>top100</th>
                    <th>خوب</th>
                    <th>همه اپ</th>
                  </tr>
                </thead>
                <tbody>
                  {data.panels.map((p) => (
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
                          {!p.is_active ? " · خاموش" : ""}
                        </div>
                      </td>
                      <td>{toFaDigits(p.online)}</td>
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
            <p className="muted" style={{ marginTop: 10 }}>
              {data.note} · ستون‌های تست: آخرین دور / جمع عمری
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
