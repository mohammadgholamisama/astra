import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowDownRight,
  ArrowUpRight,
  BookOpen,
  Clock,
  Loader2,
  Minus,
  Radio,
  RefreshCw,
  Search,
  Server,
  Smartphone,
  Wifi,
  X,
  Zap,
} from "lucide-react";
import AnalyticsGuideModal, {
  USER_ANALYTICS_GUIDE,
} from "../components/AnalyticsGuide";
import { hourLabelLocal, tzOffsetMinutes } from "../lib/analyticsTime";
import { api } from "../lib/api";
import { formatTraffic, toFaDigits } from "../lib/format";

type SearchHit = {
  username: string;
  display_name?: string | null;
  status: string;
};

type Overview = {
  generated_at: string;
  online_users: number;
  raw_hour: { current: number; previous: number; change_pct: number | null };
  raw_today: number;
  raw_current?: number;
  raw_pending?: number;
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  global_hourly: { hour_ts: number; raw_bytes: number }[];
  top_hour: { username: string; raw_bytes: number }[];
  note?: string;
};

type UserAnalytics = {
  username: string;
  display_name?: string | null;
  online: boolean;
  online_since?: string | null;
  last_sub_fetch_at?: string | null;
  raw_totals?: { current_raw?: number; pending_raw?: number };
  compare: Record<
    string,
    { current: number; previous: number; change_pct: number | null }
  >;
  current_session?: {
    duration_seconds: number;
    raw_bytes: number;
  } | null;
  hourly: {
    hour_ts: number;
    raw_bytes: number;
  }[];
  hour_of_day: { bytes: number[]; peak_hour: number | null; peak_bytes: number };
  devices: {
    count: number;
    items: { label: string; hit_count: number; last_seen_at?: string | null }[];
  };
  networks: {
    count: number;
    items: {
      label: string;
      detail?: string | null;
      hit_count: number;
      last_seen_at?: string | null;
    }[];
  };
  recent_fetches: {
    ts?: string | null;
    ip: string;
    network: string;
    app: string;
  }[];
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

function durLabel(sec: number) {
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return `${toFaDigits(s)} ثانیه`;
  if (s < 3600) return `${toFaDigits(Math.round(s / 60))} دقیقه`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return `${toFaDigits(h)}س ${toFaDigits(m)}د`;
}

/** Horizontal usage bar — native title only (no overlapping absolute tips). */
function UsageBar({
  label,
  value,
  max,
  tip,
}: {
  label: string;
  value: number;
  max: number;
  tip: string;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="an-ubar" title={tip}>
      <div className="an-ubar-top">
        <span>{label}</span>
        <b>{formatTraffic(value)}</b>
      </div>
      <div className="an-ubar-track">
        <i style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function AnalyticsPage() {
  const [params, setParams] = useSearchParams();
  const nav = useNavigate();
  const focusUser = (params.get("u") || "").trim();
  const [q, setQ] = useState(focusUser);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [freshKey, setFreshKey] = useState(0);
  const [guideOpen, setGuideOpen] = useState(false);
  const [hodHover, setHodHover] = useState<{ i: number; bytes: number } | null>(
    null
  );
  const searchWrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setQ(focusUser);
  }, [focusUser]);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (!searchWrapRef.current?.contains(e.target as Node)) {
        setSuggestOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const tz = tzOffsetMinutes();

  const overview = useQuery({
    queryKey: ["analytics-overview", tz],
    queryFn: () =>
      api<Overview>(`/api/analytics/overview?tz_offset=${tz}`),
    refetchInterval: 60_000,
  });

  const userQ = useQuery({
    queryKey: ["analytics-user", focusUser, freshKey, tz],
    queryFn: () =>
      api<UserAnalytics>(
        `/api/analytics/user/${encodeURIComponent(focusUser)}?refresh=1&tz_offset=${tz}`,
        { timeoutMs: 90_000 }
      ),
    enabled: Boolean(focusUser),
    refetchInterval: focusUser ? 60_000 : false,
    staleTime: 0,
  });

  const suggestQ = useQuery({
    queryKey: ["analytics-user-search", q.trim()],
    queryFn: () =>
      api<{ items: SearchHit[] }>(
        `/api/analytics/users/search?q=${encodeURIComponent(q.trim())}&limit=12`
      ),
    enabled: q.trim().length >= 1,
    placeholderData: (prev) => prev,
  });

  const data = overview.data;
  const maxHour = useMemo(() => {
    if (!data?.global_hourly?.length) return 1;
    return Math.max(1, ...data.global_hourly.map((h) => h.raw_bytes));
  }, [data]);
  const maxHod = useMemo(() => {
    if (!data?.hour_of_day?.bytes?.length) return 1;
    return Math.max(1, ...data.hour_of_day.bytes);
  }, [data]);

  const suggestions = suggestQ.data?.items || [];
  const ua = userQ.data;
  const refreshing = Boolean(focusUser) && (userQ.isFetching || userQ.isLoading);

  const userHourMax = useMemo(() => {
    if (!ua?.hourly?.length) return 1;
    return Math.max(1, ...ua.hourly.map((h) => h.raw_bytes));
  }, [ua]);

  function openUser(name: string) {
    const u = name.trim();
    if (!u) return;
    setSuggestOpen(false);
    setQ(u);
    setFreshKey((k) => k + 1);
    setParams({ u });
  }

  function onSearch(e: FormEvent) {
    e.preventDefault();
    const name = q.trim();
    if (!name) {
      setParams({});
      return;
    }
    openUser(name);
  }

  return (
    <div className="an-page">
      <header className="an-hero an-hero-users">
        <div className="an-hero-copy">
          <p className="an-kicker">تحلیل رفتار</p>
          <h1>آمار کاربران</h1>
          <p className="an-sub">
            مصرف خام، آنلاین بودن، دستگاه و اینترنت آپدیت ساب — ساعت‌ها محلی دستگاه شما
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
            <Link to="/panel-analytics" className="btn ghost sm">
              <Server size={14} />
              آمار پنل‌ها
            </Link>
            <button
              type="button"
              className="btn ghost sm"
              disabled={overview.isFetching}
              onClick={() => void overview.refetch()}
            >
              <RefreshCw
                size={14}
                className={overview.isFetching ? "configs-spin" : undefined}
              />
              بروزرسانی
            </button>
          </div>
        </div>

        <div className="an-search-panel" ref={searchWrapRef}>
          <label className="an-search-label" htmlFor="an-user-search">
            جستجوی کاربر
          </label>
          <form className="an-search an-search-lg" onSubmit={onSearch}>
            <Search size={18} className="an-search-ico" />
            <input
              id="an-user-search"
              ref={inputRef}
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setSuggestOpen(true);
              }}
              onFocus={() => setSuggestOpen(true)}
              placeholder="یوزرنیم یا نام نمایشی…"
              dir="ltr"
              autoComplete="off"
              spellCheck={false}
            />
            {q ? (
              <button
                type="button"
                className="an-search-clear"
                aria-label="پاک کردن"
                onClick={() => {
                  setQ("");
                  setSuggestOpen(false);
                  inputRef.current?.focus();
                }}
              >
                <X size={16} />
              </button>
            ) : null}
            <button
              type="submit"
              className="btn sm an-search-go"
              disabled={!q.trim()}
            >
              {refreshing && focusUser === q.trim() ? (
                <>
                  <Loader2 size={14} className="configs-spin" />
                  خواندن…
                </>
              ) : (
                "تحلیل"
              )}
            </button>
          </form>
          <p className="an-search-hint">
            با انتخاب، مصرف زنده از پنل‌ها همین الان گرفته می‌شود
          </p>
          {suggestOpen && q.trim().length >= 1 ? (
            <div className="an-suggest">
              {suggestQ.isFetching && suggestions.length === 0 ? (
                <div className="an-suggest-empty">
                  <Loader2 size={14} className="configs-spin" />
                  جستجو…
                </div>
              ) : suggestions.length === 0 ? (
                <div className="an-suggest-empty">
                  پیدا نشد — Enter برای تلاش مستقیم
                </div>
              ) : (
                <ul>
                  {suggestions.map((s) => (
                    <li key={s.username}>
                      <button type="button" onClick={() => openUser(s.username)}>
                        <span className="an-suggest-main">
                          <b dir="ltr">{s.username}</b>
                          {s.display_name ? <small>{s.display_name}</small> : null}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
        </div>
      </header>

      <AnalyticsGuideModal
        open={guideOpen}
        onClose={() => setGuideOpen(false)}
        title="راهنمای آمار کاربران"
        intro="فقط داده‌های واقعی مصرف و ساب اینجا هستند. مصرف‌ها بدون ضریب‌اند."
        items={USER_ANALYTICS_GUIDE}
      />

      {focusUser ? (
        <section className="an-user-panel">
          <div className="an-user-banner">
            <div className="an-user-banner-main">
              <button
                type="button"
                className="btn ghost sm"
                onClick={() => {
                  setParams({});
                  setQ("");
                }}
              >
                ← نمای کلی
              </button>
              <div className="an-user-banner-id">
                <h2 dir="ltr">{focusUser}</h2>
                {ua?.display_name ? <p>{ua.display_name}</p> : null}
              </div>
              {ua ? (
                <span className={`badge ${ua.online ? "ok" : "muted"}`}>
                  {ua.online ? "آنلاین" : "آفلاین"}
                </span>
              ) : null}
            </div>
            <div className="an-user-banner-actions">
              {refreshing ? (
                <span className="an-live-pill">
                  <Loader2 size={14} className="configs-spin" />
                  گرفتن دیتای تازه…
                </span>
              ) : null}
              <button
                type="button"
                className="btn sm"
                disabled={refreshing}
                onClick={() => setFreshKey((k) => k + 1)}
              >
                <RefreshCw
                  size={14}
                  className={refreshing ? "configs-spin" : undefined}
                />
                تازه‌سازی
              </button>
              <button
                type="button"
                className="btn ghost sm"
                onClick={() =>
                  nav(`/users?q=${encodeURIComponent(focusUser)}`)
                }
              >
                صفحه کاربر
              </button>
            </div>
            {ua ? (
              <div className="an-user-banner-meta">
                {ua.last_sub_fetch_at ? (
                  <span>
                    <Clock size={13} /> آخرین آپدیت ساب:{" "}
                    {new Date(ua.last_sub_fetch_at).toLocaleString("fa-IR")}
                  </span>
                ) : (
                  <span>هنوز آپدیت سابی دیده نشده</span>
                )}
                {ua.online_since ? (
                  <span>
                    آنلاین از{" "}
                    {new Date(ua.online_since).toLocaleString("fa-IR")}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>

          {userQ.isLoading ? (
            <div className="an-fresh-loading">
              <Loader2 size={22} className="configs-spin" />
              <div>
                <b>در حال خواندن مصرف زنده…</b>
                <p>ترافیک تازه از پنل‌ها گرفته می‌شود</p>
              </div>
            </div>
          ) : userQ.isError ? (
            <div className="admin-empty">
              <p>کاربر پیدا نشد یا خطا در دریافت</p>
              <button
                type="button"
                className="btn sm"
                onClick={() => setFreshKey((k) => k + 1)}
              >
                تلاش مجدد
              </button>
            </div>
          ) : ua ? (
            <div className="an-user-stack">
              <div className="an-stat-grid">
                <article className="an-stat">
                  <span>وضعیت</span>
                  <strong>{ua.online ? "آنلاین" : "آفلاین"}</strong>
                </article>
                <article className="an-stat">
                  <span>
                    <Zap size={13} /> مصرف فعلی
                  </span>
                  <strong>
                    {formatTraffic(Number(ua.raw_totals?.current_raw || 0))}
                  </strong>
                  <small>خام · بدون ضریب</small>
                </article>
                <article className="an-stat">
                  <span>
                    <Smartphone size={13} /> دستگاه ساب
                  </span>
                  <strong>{toFaDigits(ua.devices.count)}</strong>
                </article>
                <article className="an-stat">
                  <span>
                    <Wifi size={13} /> اینترنت
                  </span>
                  <strong>{toFaDigits(ua.networks.count)}</strong>
                </article>
              </div>

              <div className="an-grid-2">
                <section className="an-block">
                  <h3>مقایسه مصرف خام</h3>
                  {(["hour", "day", "week", "month"] as const).map((k) => {
                    const row = ua.compare[k];
                    const title =
                      k === "hour"
                        ? "این ساعت / ساعت قبل"
                        : k === "day"
                          ? "امروز / دیروز"
                          : k === "week"
                            ? "این هفته / هفته قبل"
                            : "این ماه / ماه قبل";
                    return (
                      <div key={k} className="an-compare-row">
                        <div>
                          <b>{title}</b>
                          <p>
                            {formatTraffic(row.current)}{" "}
                            <span className="muted">در برابر</span>{" "}
                            {formatTraffic(row.previous)}
                          </p>
                        </div>
                        <ChangePill pct={row.change_pct} />
                      </div>
                    );
                  })}
                </section>

                <section className="an-block">
                  <h3>نشست فعلی</h3>
                  {ua.current_session ? (
                    <div className="an-session">
                      <p>
                        مدت {durLabel(ua.current_session.duration_seconds)}
                      </p>
                      <p>
                        مصرف {formatTraffic(ua.current_session.raw_bytes)}
                      </p>
                    </div>
                  ) : (
                    <p className="muted">الان آنلاین تشخیص داده نشده</p>
                  )}
                </section>
              </div>

              <section className="an-block">
                <div className="an-block-head">
                  <h3>مصرف ساعتی — ۴۸ ساعت اخیر</h3>
                  <span className="muted">زمان محلی دستگاه</span>
                </div>
                <div className="an-bars">
                  {ua.hourly.map((h) => (
                    <UsageBar
                      key={h.hour_ts}
                      label={hourLabelLocal(h.hour_ts)}
                      value={h.raw_bytes}
                      max={userHourMax}
                      tip={`${hourLabelLocal(h.hour_ts)} — ${formatTraffic(h.raw_bytes)}`}
                    />
                  ))}
                </div>
              </section>

              <section className="an-block">
                <div className="an-block-head">
                  <h3>مصرف در ساعات شبانه‌روز</h3>
                  <span className="muted">ساعت محلی</span>
                </div>
                <div className="an-hod an-hod-live">
                  {ua.hour_of_day.bytes.map((b, i) => {
                    const pct = (b / Math.max(1, ...ua.hour_of_day.bytes)) * 100;
                    const peak = ua.hour_of_day.peak_hour === i;
                    return (
                      <button
                        type="button"
                        key={i}
                        className={`an-hod-col${peak ? " peak" : ""}`}
                        onMouseEnter={() => setHodHover({ i, bytes: b })}
                        onMouseLeave={() => setHodHover(null)}
                        onFocus={() => setHodHover({ i, bytes: b })}
                        onBlur={() => setHodHover(null)}
                      >
                        <i style={{ height: `${Math.max(6, pct)}%` }} />
                        <span>{toFaDigits(i)}</span>
                      </button>
                    );
                  })}
                </div>
                <div className="an-hod-legend">
                  {hodHover ? (
                    <b>
                      ساعت {toFaDigits(hodHover.i)}:۰۰ —{" "}
                      {formatTraffic(hodHover.bytes)}
                    </b>
                  ) : ua.hour_of_day.peak_hour != null ? (
                    <span>
                      اوج: ساعت {toFaDigits(ua.hour_of_day.peak_hour)} (
                      {formatTraffic(ua.hour_of_day.peak_bytes)})
                    </span>
                  ) : (
                    <span className="muted">روی ستون‌ها بروید</span>
                  )}
                </div>
              </section>

              <div className="an-grid-2">
                <section className="an-block">
                  <h3>
                    <Smartphone size={16} /> کلاینت‌های ساب
                  </h3>
                  {ua.devices.items.length === 0 ? (
                    <p className="muted">هنوز آپدیت سابی ثبت نشده</p>
                  ) : (
                    <ul className="an-list">
                      {ua.devices.items.map((d, i) => (
                        <li key={i}>
                          <b>{d.label}</b>
                          <span className="badge muted">
                            {toFaDigits(d.hit_count)} بار
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
                <section className="an-block">
                  <h3>
                    <Wifi size={16} /> اینترنت‌ها
                  </h3>
                  {ua.networks.items.length === 0 ? (
                    <p className="muted">شبکه‌ای ثبت نشده</p>
                  ) : (
                    <ul className="an-list">
                      {ua.networks.items.map((n, i) => (
                        <li key={i}>
                          <div>
                            <b dir="ltr">{n.label}</b>
                            {n.detail ? (
                              <small className="muted" dir="ltr">
                                {n.detail}
                              </small>
                            ) : null}
                          </div>
                          <span className="badge muted">
                            {toFaDigits(n.hit_count)} بار
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              </div>

              {ua.recent_fetches.length > 0 ? (
                <section className="an-block">
                  <h3>آخرین آپدیت‌های ساب</h3>
                  <div className="an-table-wrap">
                    <table className="an-table">
                      <thead>
                        <tr>
                          <th>زمان</th>
                          <th>اپ</th>
                          <th>IP</th>
                          <th>شبکه</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ua.recent_fetches.slice(0, 15).map((f, i) => (
                          <tr key={i}>
                            <td>
                              {f.ts
                                ? new Date(f.ts).toLocaleString("fa-IR")
                                : "—"}
                            </td>
                            <td>{f.app}</td>
                            <td dir="ltr">{f.ip}</td>
                            <td dir="ltr">{f.network}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : overview.isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : data ? (
        <>
          <div className="an-stat-grid">
            <article className="an-stat">
              <span>
                <Radio size={13} /> آنلاین الان
              </span>
              <strong>{toFaDigits(data.online_users)}</strong>
              <small>مصرف اخیر روی پنل‌ها</small>
            </article>
            <article className="an-stat">
              <span>
                <Zap size={13} /> مصرف فعلی کل
              </span>
              <strong>
                {formatTraffic(Number(data.raw_current || 0))}
              </strong>
              <small>جمع خام اکانت‌ها</small>
            </article>
            <article className="an-stat">
              <span>این ساعت / امروز</span>
              <strong>{formatTraffic(data.raw_hour.current)}</strong>
              <small>
                امروز {formatTraffic(data.raw_today)}{" "}
                <ChangePill pct={data.raw_hour.change_pct} />
              </small>
            </article>
            <article className="an-stat">
              <span>اوج ساعات روز</span>
              <strong>
                {data.hour_of_day.peak_hour != null
                  ? `ساعت ${toFaDigits(data.hour_of_day.peak_hour)}`
                  : "—"}
              </strong>
              <small>{formatTraffic(data.hour_of_day.peak_bytes)}</small>
            </article>
          </div>

          <div className="an-grid-2">
            <section className="an-block">
              <div className="an-block-head">
                <h3>مصرف کل — ۲۴ ساعت</h3>
                <span className="muted">هاور روی ردیف</span>
              </div>
              <div className="an-bars">
                {data.global_hourly.map((h) => (
                  <UsageBar
                    key={h.hour_ts}
                    label={hourLabelLocal(h.hour_ts)}
                    value={h.raw_bytes}
                    max={maxHour}
                    tip={`${hourLabelLocal(h.hour_ts)} — ${formatTraffic(h.raw_bytes)}`}
                  />
                ))}
              </div>
            </section>
            <section className="an-block">
              <h3>پرمصرف‌ترین‌ها در این ساعت</h3>
              {data.top_hour.length === 0 ? (
                <p className="muted">هنوز دلتایی ثبت نشده</p>
              ) : (
                <ul className="an-list">
                  {data.top_hour.map((u) => (
                    <li key={u.username}>
                      <button
                        type="button"
                        className="an-linkish"
                        onClick={() => openUser(u.username)}
                      >
                        <b dir="ltr">{u.username}</b>
                      </button>
                      <span>{formatTraffic(u.raw_bytes)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <section className="an-block">
            <div className="an-block-head">
              <h3>توزیع مصرف در ساعات روز (۷ روز)</h3>
              <span className="muted">هاور = جزئیات</span>
            </div>
            <div className="an-hod an-hod-live">
              {data.hour_of_day.bytes.map((b, i) => {
                const pct = (b / maxHod) * 100;
                const peak = data.hour_of_day.peak_hour === i;
                return (
                  <button
                    type="button"
                    key={i}
                    className={`an-hod-col${peak ? " peak" : ""}`}
                    onMouseEnter={() => setHodHover({ i, bytes: b })}
                    onMouseLeave={() => setHodHover(null)}
                    onFocus={() => setHodHover({ i, bytes: b })}
                    onBlur={() => setHodHover(null)}
                  >
                    <i style={{ height: `${Math.max(6, pct)}%` }} />
                    <span>{toFaDigits(i)}</span>
                  </button>
                );
              })}
            </div>
            <div className="an-hod-legend">
              {hodHover ? (
                <b>
                  ساعت {toFaDigits(hodHover.i)}:۰۰ —{" "}
                  {formatTraffic(hodHover.bytes)}
                </b>
              ) : (
                <span className="muted">روی ستون‌ها بروید تا مقدار دیده شود</span>
              )}
            </div>
          </section>
        </>
      ) : (
        <div className="admin-empty">
          <p>خطا در بارگذاری آمار</p>
        </div>
      )}
    </div>
  );
}
