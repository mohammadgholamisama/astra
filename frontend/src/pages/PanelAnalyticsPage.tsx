import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Gauge,
  Loader2,
  RefreshCw,
  Server,
  Users,
  X,
  Zap,
} from "lucide-react";
import AnalyticsGuideModal, {
  PANEL_ANALYTICS_GUIDE,
} from "../components/AnalyticsGuide";
import { hourLabelLocal, tzOffsetMinutes } from "../lib/analyticsTime";
import { api } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

type WeightedSlice = {
  current_total?: number;
  hour?: number;
  day?: number;
};

type PanelRow = {
  panel_id: number;
  name: string;
  type: string;
  is_active: boolean;
  accounts?: number;
  online: number;
  raw: {
    current_total?: number;
    pending?: number;
    hour: number;
    day: number;
    week: number;
    month: number;
    lifetime_cursor: number;
  };
  weighted?: WeightedSlice;
  compare_hour_pct: number | null;
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

type TopHit = {
  panel_id: number;
  name: string;
  raw_bytes: number;
  metric?: string;
};

type Overview = {
  generated_at: string;
  show_online?: boolean;
  online: {
    available?: boolean;
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
    weighted_current?: number;
    raw_pending?: number;
    raw_hour: number;
    weighted_hour?: number;
    raw_day: number;
    weighted_day?: number;
    panels: number;
  };
  top?: {
    by_current?: TopHit | null;
    by_hour?: TopHit | null;
    by_day?: TopHit | null;
  };
  top_day_panels?: {
    panel_id: number;
    name: string;
    raw_bytes: number;
    current_total: number;
    online: number;
  }[];
  global_hourly: {
    hour_ts: number;
    raw_bytes: number;
    weighted_bytes?: number;
    is_current?: boolean;
  }[];
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  panels: PanelRow[];
  note?: string;
};

type Detail = {
  panel_id: number;
  name: string;
  type: string;
  accounts: number;
  show_online?: boolean;
  online: {
    online: number;
    operators: Record<string, { count: number; label: string }>;
    brands: Record<string, number>;
  };
  lifetime_raw: number;
  current_total?: number;
  current_weighted?: number;
  pending_raw?: number;
  weighted?: WeightedSlice;
  compare: Record<
    string,
    {
      current: number;
      previous: number;
      change_pct: number | null;
      weighted?: number;
    }
  >;
  hourly: {
    hour_ts: number;
    raw_bytes: number;
    avg_online: number;
    online_peak: number;
  }[];
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  tests: {
    runs: number;
    configs_tested: number;
    top100: number;
    good: number;
    all_ops_ok: number;
    last_run?: {
      tested: number;
      in_top100: number;
      good: number;
      all_ops_ok: number;
      avg_ping: number;
      ts?: string | null;
    } | null;
    history: {
      ts?: string | null;
      tested: number;
      in_top100: number;
      good: number;
      all_ops_ok: number;
      avg_ping: number;
    }[];
  };
};

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

type HourPoint = {
  hour_ts: number;
  raw_bytes: number;
  weighted_bytes?: number;
  is_current?: boolean;
};

function hourValue(p: HourPoint, mode: "weighted" | "raw"): number {
  if (mode === "raw") return Number(p.raw_bytes || 0);
  const w = Number(p.weighted_bytes ?? 0);
  return w > 0 ? w : Number(p.raw_bytes || 0);
}

function clockLabel(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("fa-IR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Vertical 24h column chart — local time; primary = با ضریب. */
function HourlyUsageChart({
  points,
  mode = "weighted",
  currentHourBytes,
  currentHourRaw,
}: {
  points?: HourPoint[] | null;
  mode?: "weighted" | "raw";
  /** Authoritative «این ساعت» from Astra live watermarks (overrides chart bar). */
  currentHourBytes?: number | null;
  currentHourRaw?: number | null;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const series = points ?? [];
  if (!series.length) {
    return <p className="muted">هنوز دادهٔ ساعتی برای نمودار نیست</p>;
  }
  const vals = series.map((p) => hourValue(p, mode));
  const max = Math.max(1, ...vals);
  const total = vals.reduce((s, v) => s + v, 0);
  const totalRaw = series.reduce((s, p) => s + Number(p.raw_bytes || 0), 0);
  const peakIdx = vals.reduce(
    (best, v, i, arr) => (v > arr[best] ? i : best),
    0
  );
  const nowIdx = series.findIndex((p) => p.is_current);
  const currentIdx = nowIdx >= 0 ? nowIdx : series.length - 1;
  const peak = series[peakIdx];
  const peakVal = vals[peakIdx];
  const barCurrent = vals[currentIdx] || 0;
  const currentVal =
    currentHourBytes != null && Number.isFinite(Number(currentHourBytes))
      ? Math.max(0, Number(currentHourBytes))
      : barCurrent;
  const tipIdx = hover != null ? hover : currentIdx;
  const tip = series[tipIdx];
  const tipVal =
    tip && tip.is_current && currentHourBytes != null
      ? Math.max(0, Number(currentHourBytes))
      : tip
        ? hourValue(tip, mode)
        : 0;
  const tipIsNow = Boolean(tip?.is_current);
  const hourWeightedHint =
    currentHourRaw != null &&
    Number(currentHourRaw) > 0 &&
    Math.abs(Number(currentHourRaw) - currentVal) > 1
      ? Number(currentHourRaw)
      : null;

  return (
    <div className="pan-chart">
      <div className="pan-chart-meta">
        <div>
          <span>{mode === "weighted" ? "جمع ۲۴س · با ضریب" : "جمع ۲۴س · خام"}</span>
          <b>{formatTraffic(total)}</b>
          {mode === "weighted" &&
          totalRaw > 0 &&
          Math.abs(totalRaw - total) > 1 ? (
            <small>خام {formatTraffic(totalRaw)}</small>
          ) : null}
        </div>
        <div>
          <span>این ساعت</span>
          <b>{formatTraffic(currentVal)}</b>
          <small>
            از {clockLabel(series[currentIdx]?.hour_ts || 0)} تا الان · خام Astra
            {hourWeightedHint != null
              ? ` · با ضریب ${formatTraffic(hourWeightedHint)}`
              : ""}
          </small>
        </div>
        <div>
          <span>اوج ۲۴س</span>
          <b>
            {peakVal > 0 ? formatTraffic(peakVal) : "—"}
            {peak && peakVal > 0 ? (
              <small> · {clockLabel(peak.hour_ts)}</small>
            ) : null}
          </b>
        </div>
      </div>

      <div className="pan-chart-tip" aria-live="polite">
        {tip ? (
          <>
            <b>
              {tipIsNow ? "اکنون · " : ""}
              {hourLabelLocal(tip.hour_ts)}
            </b>
            <span>{formatTraffic(tipVal)}</span>
            {mode === "weighted" &&
            Number(tip.raw_bytes || 0) > 0 &&
            Math.abs(Number(tip.raw_bytes || 0) - tipVal) > 1 ? (
              <small>خام {formatTraffic(Number(tip.raw_bytes || 0))}</small>
            ) : max > 0 && tipVal > 0 ? (
              <small>
                {toFaDigits(((tipVal / max) * 100).toFixed(0))}٪ از اوج
              </small>
            ) : null}
          </>
        ) : (
          <span className="muted">روی ستون‌ها بروید تا جزئیات دیده شود</span>
        )}
      </div>

      <div className="pan-chart-plot" role="img" aria-label="نمودار مصرف ۲۴ ساعت">
        <div className="pan-chart-y">
          <span>{formatTraffic(max)}</span>
          <span>{formatTraffic(max / 2)}</span>
          <span>۰</span>
        </div>
        <div className="pan-chart-cols">
          {series.map((p, i) => {
            const v = vals[i];
            const h = Math.max(v > 0 ? 5 : 0, (v / max) * 100);
            const isPeak = i === peakIdx && v > 0;
            const isNow = Boolean(p.is_current);
            const isHover = hover === i;
            const showLabel =
              i === 0 ||
              i === series.length - 1 ||
              i % 2 === 0 ||
              isNow;
            return (
              <button
                type="button"
                key={p.hour_ts}
                className={`pan-chart-col${isPeak ? " is-peak" : ""}${
                  isNow ? " is-now" : ""
                }${isHover ? " is-hover" : ""}`}
                style={{ ["--h" as string]: `${h}%` }}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
                title={`${hourLabelLocal(p.hour_ts)} — ${formatTraffic(v)}`}
              >
                <i />
                {showLabel ? (
                  <span className={isNow ? "is-now-label" : undefined}>
                    {clockLabel(p.hour_ts)}
                  </span>
                ) : (
                  <span className="pan-chart-tick" />
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function PanelDetailModal({
  panel,
  detail,
  loading,
  onClose,
}: {
  panel: PanelRow;
  detail?: Detail | null;
  loading: boolean;
  onClose: () => void;
}) {
  const current = Number(
    detail?.current_total ?? panel.raw?.current_total ?? 0
  );
  const currentW = Number(
    detail?.current_weighted ??
      detail?.weighted?.current_total ??
      panel.weighted?.current_total ??
      0
  );
  const accounts = Number(detail?.accounts ?? panel.accounts ?? 0);
  const lastRun = detail?.tests?.last_run ?? panel.tests?.last_run ?? null;
  const hasTest = Boolean(lastRun && Number(lastRun.tested || 0) > 0);
  const hourly = detail?.hourly ?? [];
  const hasHourly = hourly.some((p) => Number(p.raw_bytes || 0) > 0);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-lg pan-detail-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={panel.name}
      >
        <header className="modal-head">
          <div>
            <h3>{panel.name}</h3>
            <p className="modal-sub">
              {panel.type}
              {accounts > 0 ? ` · ${toFaDigits(accounts)} اکانت` : ""}
              {!panel.is_active ? " · خاموش" : ""}
            </p>
          </div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="بستن"
          >
            <X size={18} />
          </button>
        </header>

        <div className="pan-modal-stats pan-modal-stats-2">
          <article>
            <span>مصرف فعلی · بدون ضریب</span>
            <b>{formatTraffic(current)}</b>
            <small>جمع خام اکانت‌های Astra · کل عمر</small>
          </article>
          <article>
            <span>مصرف فعلی · با ضریب</span>
            <b>{formatTraffic(currentW > 0 ? currentW : current)}</b>
            <small>Σ(خام × ضریب گروه)</small>
          </article>
        </div>

        {hasTest ? (
          <div className="pan-modal-test">
            <h4>آخرین تست پینگ</h4>
            <div className="pan-modal-test-grid">
              <span>
                تست‌شده: <b>{toFaDigits(lastRun!.tested)}</b>
              </span>
              <span>
                top100: <b>{toFaDigits(lastRun!.in_top100)}</b>
              </span>
              <span>
                خوب: <b>{toFaDigits(lastRun!.good)}</b>
              </span>
              {lastRun!.avg_ping > 0 ? (
                <span dir="ltr">
                  پینگ: <b>{lastRun!.avg_ping.toFixed(0)} ms</b>
                </span>
              ) : null}
            </div>
            {lastRun!.ts ? (
              <small className="muted">
                {new Date(lastRun!.ts).toLocaleString("fa-IR")}
              </small>
            ) : null}
          </div>
        ) : null}

        {loading ? (
          <div className="an-fresh-loading" style={{ marginTop: 12 }}>
            <Loader2 size={18} className="configs-spin" />
            <div>
              <b>بارگذاری نمودار…</b>
            </div>
          </div>
        ) : hasHourly ? (
          <div className="pan-modal-chart">
            <h4>۴۸ ساعت اخیر · با ضریب</h4>
            <HourlyUsageChart points={hourly} mode="weighted" />
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function PanelAnalyticsPage() {
  const qc = useQueryClient();
  const [focusId, setFocusId] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [sortKey, setSortKey] = useState<"total" | "weighted">("total");
  const [guideOpen, setGuideOpen] = useState(false);

  const tz = tzOffsetMinutes();

  const overview = useQuery({
    queryKey: ["analytics-panels", tz],
    queryFn: () =>
      api<Overview>(`/api/analytics/panels?tz_offset=${tz}`, {
        timeoutMs: 45_000,
      }),
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
    retry: 2,
    staleTime: 10_000,
  });

  const detail = useQuery({
    queryKey: ["analytics-panel", focusId, tz],
    queryFn: () =>
      api<Detail>(`/api/analytics/panels/${focusId}?tz_offset=${tz}`, {
        timeoutMs: 20_000,
      }),
    enabled: focusId != null,
    refetchInterval: false,
  });

  const liveRefresh = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; stats: Record<string, number>; overview: Overview }>(
        `/api/analytics/panels/refresh?tz_offset=${tz}`,
        { method: "POST", timeoutMs: 180_000 }
      ),
    onSuccess: (res) => {
      if (res.overview) {
        qc.setQueryData(["analytics-panels", tz], res.overview);
      } else {
        void qc.invalidateQueries({ queryKey: ["analytics-panels"] });
      }
      if (focusId != null) {
        void qc.invalidateQueries({ queryKey: ["analytics-panel", focusId] });
      }
    },
  });

  const data = overview.data;
  const maxPanelTotal = useMemo(() => {
    if (!data?.panels?.length) return 1;
    return Math.max(
      1,
      ...data.panels.map((p) => Number(p.raw.current_total || p.raw.hour || 0))
    );
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
      if (sortKey === "weighted") {
        return (
          Number(b.weighted?.current_total || 0) -
          Number(a.weighted?.current_total || 0)
        );
      }
      return (
        Number(b.raw?.current_total || 0) - Number(a.raw?.current_total || 0)
      );
    });
    return list;
  }, [data, q, sortKey]);

  const focusedPanel = useMemo((): PanelRow | null => {
    if (focusId == null) return null;
    const fromList = data?.panels?.find((p) => p.panel_id === focusId);
    if (fromList) return fromList;
    const d = detail.data;
    if (d && d.panel_id === focusId) {
      return {
        panel_id: d.panel_id,
        name: d.name,
        type: d.type,
        is_active: true,
        accounts: d.accounts,
        online: d.online.online,
        raw: {
          current_total: Number(d.current_total ?? d.lifetime_raw ?? 0),
          hour: d.compare.hour.current,
          day: d.compare.day.current,
          week: d.compare.week?.current ?? 0,
          month: d.compare.month?.current ?? 0,
          lifetime_cursor: Number(d.lifetime_raw ?? 0),
        },
        compare_hour_pct: d.compare.hour.change_pct,
        tests: d.tests,
      };
    }
    return {
      panel_id: focusId,
      name: `پنل #${focusId}`,
      type: "—",
      is_active: true,
      online: 0,
      raw: {
        current_total: 0,
        hour: 0,
        day: 0,
        week: 0,
        month: 0,
        lifetime_cursor: 0,
      },
      compare_hour_pct: null,
      tests: {
        runs: 0,
        configs_tested: 0,
        top100: 0,
        good: 0,
        all_ops_ok: 0,
        last_run: null,
      },
    };
  }, [data, detail.data, focusId]);

  useEffect(() => {
    if (focusId == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFocusId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusId]);

  const refreshing = liveRefresh.isPending;

  return (
    <div className="an-page">
      <header className="an-hero">
        <div>
          <p className="an-kicker">تحلیل زیر‌ساخت</p>
          <h1>آمار پنل‌ها</h1>
          <p className="an-sub">
            فقط آمار دقیق از اکانت‌های Astra — خام و با ضریب
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
        intro="عدد اصلی خام است؛ با ضریب ریز زیرش می‌آید. تست پینگ از ping-hub."
        items={PANEL_ANALYTICS_GUIDE}
      />

      {overview.isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : overview.isError ? (
        <div className="admin-empty">
          <p>خطا در بارگذاری آمار پنل‌ها</p>
          <p className="muted" style={{ marginTop: 6 }}>
            {(overview.error as Error)?.message || "پاسخ سرور ناموفق بود"}
          </p>
          <button
            type="button"
            className="btn sm"
            style={{ marginTop: 12 }}
            onClick={() => void overview.refetch()}
          >
            تلاش مجدد
          </button>
        </div>
      ) : data ? (
        <>
          {refreshing ? (
            <div className="an-fresh-loading">
              <Loader2 size={22} className="configs-spin" />
              <div>
                <b>در حال خواندن مصرف زنده از پنل‌ها…</b>
                <p>صفحه قابل استفاده است — بعد از اتمام عددها تازه می‌شوند</p>
              </div>
            </div>
          ) : null}

          {data.top?.by_current ? (
            <div className="an-top-callout">
              <Zap size={16} />
              <div>
                <b>بیشترین مصرف: {data.top.by_current.name}</b>
                <small>بر اساس مصرف فعلی (خام · کل عمر)</small>
              </div>
              <span className="an-top-val">
                {formatTraffic(Number(data.top.by_current.raw_bytes || 0))}
              </span>
              <button
                type="button"
                className="btn ghost sm"
                onClick={() =>
                  setFocusId(Number(data.top?.by_current?.panel_id))
                }
              >
                جزئیات
              </button>
            </div>
          ) : null}

          <div className="an-stat-grid pan-stat-accurate">
            <article
              className="an-stat"
              title="Σ PanelAccount.used_traffic از اکانت‌های Astra"
            >
              <span>
                <Zap size={13} /> مصرف کل · بدون ضریب
              </span>
              <strong>
                {formatTraffic(Number(data.totals?.raw_current || 0))}
              </strong>
              <small>جمع خام اکانت‌ها</small>
            </article>
            <article
              className="an-stat"
              title="همان ترافیک مصرفی داشبورد = Σ User.used_traffic"
            >
              <span>
                <Zap size={13} /> مصرف کل · با ضریب
              </span>
              <strong>
                {formatTraffic(Number(data.totals?.weighted_current || 0))}
              </strong>
              <small>مثل داشبورد</small>
            </article>
            <article className="an-stat">
              <span>
                <Server size={13} /> پنل‌ها
              </span>
              <strong>{toFaDigits(data.totals?.panels || 0)}</strong>
              <small>{toFaDigits(panelsView.length)} در لیست</small>
            </article>
          </div>

          <section className="an-block pan-chart-block">
            <div className="an-block-head">
              <h3>مصرف کل پنل‌ها — ۲۴ ساعت</h3>
              <span className="muted">
                با ضریب · زنده با هر رفرش · زمان محلی
              </span>
            </div>
            <HourlyUsageChart
              points={data.global_hourly || []}
              mode="weighted"
              currentHourBytes={Number(data.totals?.raw_hour || 0)}
              currentHourRaw={
                data.totals?.weighted_hour != null &&
                Math.abs(
                  Number(data.totals.weighted_hour) -
                    Number(data.totals?.raw_hour || 0)
                ) > 1
                  ? Number(data.totals.weighted_hour)
                  : null
              }
            />
          </section>

          <section className="an-block">
            <div className="an-block-head">
              <h3>
                <Gauge size={16} /> سهم مصرف فعلی پنل‌ها
              </h3>
              <span className="muted">کلیک = جزئیات</span>
            </div>
            <p className="pan-share-note">
              بازه: <b>کل عمر</b> — جمع خام اکانت‌های هر پنل
            </p>
            <div className="an-bars">
              {panelsView
                .filter((p) => Number(p.raw.current_total || 0) > 0)
                .map((p, i) => {
                  const total = Number(p.raw.current_total || 0);
                  const weighted = Number(p.weighted?.current_total || 0);
                  return (
                    <button
                      key={p.panel_id}
                      type="button"
                      className="pan-bar-btn"
                      onClick={() => setFocusId(p.panel_id)}
                      title={`${p.name} — ${formatTraffic(total)}`}
                    >
                      <BarRow
                        label={`${i === 0 ? "★ " : ""}${p.name}`}
                        value={total}
                        max={maxPanelTotal}
                        sub={
                          weighted > 0 && Math.abs(weighted - total) > 1
                            ? `${formatTraffic(total)} خام · با ضریب ${formatTraffic(weighted)}`
                            : formatTraffic(total)
                        }
                      />
                    </button>
                  );
                })}
              {panelsView.every(
                (p) => Number(p.raw.current_total || 0) <= 0
              ) ? (
                <p className="muted">
                  هنوز مصرفی نیست — «تازه‌سازی زنده» را بزنید
                </p>
              ) : null}
            </div>
          </section>

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
                    ["total", "بدون ضریب"],
                    ["weighted", "با ضریب"],
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
            <div className="an-table-wrap pan-table-clean">
              <table className="an-table">
                <thead>
                  <tr>
                    <th>پنل</th>
                    <th title="جمع خام اکانت‌های Astra · کل عمر">بدون ضریب</th>
                    <th title="Σ(خام × ضریب گروه)">با ضریب</th>
                    <th>اکانت</th>
                  </tr>
                </thead>
                <tbody>
                  {panelsView.map((p, idx) => {
                    const current = Number(p.raw?.current_total || 0);
                    const currentW = Number(p.weighted?.current_total || 0);
                    const isTop = idx === 0 && current > 0;
                    return (
                      <tr
                        key={p.panel_id}
                        className={isTop ? "is-top" : undefined}
                        onClick={() => setFocusId(p.panel_id)}
                        style={{ cursor: "pointer" }}
                      >
                        <td>
                          <b>
                            {isTop ? "★ " : ""}
                            {p.name}
                          </b>
                          <div className="muted" style={{ fontSize: "0.75rem" }}>
                            {p.type}
                            {!p.is_active ? " · خاموش" : ""}
                          </div>
                        </td>
                        <td>
                          <b>{formatTraffic(current)}</b>
                        </td>
                        <td>
                          {formatTraffic(
                            currentW > 0 ? currentW : current
                          )}
                        </td>
                        <td>
                          {p.accounts != null
                            ? toFaDigits(p.accounts)
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {panelsView.length === 0 ? (
              <p className="muted" style={{ marginTop: 10 }}>
                پنلی با این جستجو پیدا نشد
              </p>
            ) : null}
            <p className="muted" style={{ marginTop: 10 }}>
              فقط اعداد دقیق از اکانت‌های Astra · کلیک روی ردیف = جزئیات
            </p>
          </section>
        </>
      ) : (
        <div className="admin-empty">
          <p>خطا در بارگذاری آمار پنل‌ها</p>
        </div>
      )}

      {focusId != null && focusedPanel ? (
        <PanelDetailModal
          panel={focusedPanel}
          detail={
            detail.data && detail.data.panel_id === focusId
              ? detail.data
              : null
          }
          loading={detail.isLoading || detail.isFetching}
          onClose={() => setFocusId(null)}
        />
      ) : null}
    </div>
  );
}
