import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, useParams } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  ClipboardPaste,
  Copy,
  ExternalLink,
  Eye,
  EyeOff,
  Gauge,
  Power,
  Radio,
  RefreshCw,
  Search,
  Server,
  Shield,
  Trash2,
  Users,
  Wifi,
  WifiOff,
  XCircle,
  Zap,
} from "lucide-react";
import {
  api,
  AstraNodeClient,
  AstraOutbound,
  formatBytes,
  Panel,
} from "../lib/api";
import { toFaDigits } from "../lib/format";

type Filter = "all" | "ok" | "down" | "off";
type Tab = "outbounds" | "clients";

function latencyTone(ms: number | null | undefined): "fast" | "mid" | "slow" | "dead" | "none" {
  if (ms == null || ms < 0) return "none";
  if (ms < 180) return "fast";
  if (ms < 450) return "mid";
  return "slow";
}

function latencyPct(ms: number | null | undefined): number {
  if (ms == null || ms < 0) return 0;
  return Math.max(8, Math.min(100, Math.round(100 - (ms / 800) * 92)));
}

function securityOf(uri: string): string {
  const m = uri.match(/[?&]security=([^&#]+)/i);
  return decodeURIComponent(m?.[1] || "none").toLowerCase();
}

function fmtTraffic(n: number): string {
  if (!n || n <= 0) return "۰ B";
  return toFaDigits(formatBytes(n).replace("∞", "0 B"));
}

function usagePct(used: number, limit: number): number {
  if (!limit || limit <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((used / limit) * 100)));
}

function statusLabel(s: string | null | undefined): string {
  switch ((s || "").toLowerCase()) {
    case "active":
      return "فعال";
    case "limited":
      return "محدود";
    case "expired":
      return "منقضی";
    case "disabled":
      return "خاموش";
    case "on_hold":
      return "معلق";
    case "pending":
      return "در صف";
    case "error":
      return "خطا";
    default:
      return s || "—";
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

export default function OutboundsPage() {
  const { panelId } = useParams();
  const id = Number(panelId);
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("outbounds");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [cq, setCq] = useState("");
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [pingingId, setPingingId] = useState<number | null>(null);
  const [subOpenId, setSubOpenId] = useState<number | null>(null);

  const { data: panels = [] } = useQuery({
    queryKey: ["panels"],
    queryFn: () => api<Panel[]>("/api/admin/panels"),
  });
  const panel = useMemo(() => panels.find((p) => p.id === id), [panels, id]);

  const { data: rows = [], isLoading, refetch, isFetching } = useQuery({
    queryKey: ["astra-outbounds", id],
    queryFn: () => api<AstraOutbound[]>(`/api/admin/panels/${id}/outbounds`),
    enabled: Number.isFinite(id) && id > 0,
  });

  const {
    data: clients = [],
    isLoading: clientsLoading,
    refetch: refetchClients,
    isFetching: clientsFetching,
  } = useQuery({
    queryKey: ["astra-clients", id],
    queryFn: () => api<AstraNodeClient[]>(`/api/admin/panels/${id}/clients`),
    enabled: Number.isFinite(id) && id > 0,
    refetchInterval: tab === "clients" ? 20000 : false,
  });

  const { data: health } = useQuery({
    queryKey: ["astra-health", id],
    queryFn: () =>
      api<{
        ok: boolean;
        listen_open: boolean;
        listen_port: number;
        public_host: string;
        public_port: number;
        public_ip?: string;
        ws_path?: string;
        inbound?: string;
        config_exists: boolean;
        dns_ok?: boolean;
        dns_note?: string;
      }>(`/api/admin/panels/${id}/astra-health`),
    enabled: Number.isFinite(id) && id > 0,
    refetchInterval: 12000,
  });

  const hasOutbound = rows.length > 0;

  function flash(ok: boolean, text: string) {
    setMsg({ ok, text });
  }

  const add = useMutation({
    mutationFn: () =>
      api<{ created: AstraOutbound[]; errors: string[]; total_created: number }>(
        `/api/admin/panels/${id}/outbounds`,
        { method: "POST", body: JSON.stringify({ text }) }
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["astra-outbounds", id] });
      qc.invalidateQueries({ queryKey: ["astra-health", id] });
      setText("");
      if (res.total_created > 0) {
        const errBit = res.errors?.length
          ? ` · ${toFaDigits(res.errors.length)} هشدار`
          : "";
        flash(true, `اوتباند اضافه شد${errBit}`);
      } else {
        flash(false, res.errors?.[0] || "افزودن ناموفق بود");
      }
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const pingAll = useMutation({
    mutationFn: () =>
      api<AstraOutbound[]>(`/api/admin/panels/${id}/outbounds/ping`, { method: "POST" }),
    onSuccess: (list) => {
      qc.setQueryData(["astra-outbounds", id], list);
      qc.invalidateQueries({ queryKey: ["astra-health", id] });
      const ok = list.filter((r) => r.last_ping_ok).length;
      flash(true, `تست Reality: ${toFaDigits(ok)} سالم از ${toFaDigits(list.length)}`);
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const pingOne = useMutation({
    mutationFn: (oid: number) =>
      api<AstraOutbound>(`/api/admin/panels/${id}/outbounds/${oid}/ping`, {
        method: "POST",
      }),
    onMutate: (oid) => setPingingId(oid),
    onSettled: () => setPingingId(null),
    onSuccess: (row) => {
      qc.setQueryData<AstraOutbound[]>(["astra-outbounds", id], (cur) =>
        (cur || []).map((r) => (r.id === row.id ? row : r))
      );
      flash(
        row.last_ping_ok === true,
        row.last_ping_ok
          ? `${row.remark || row.id}: ${toFaDigits(row.last_ping_ms ?? 0)} ms`
          : `${row.remark || row.id}: Reality ناموفق`
      );
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const rebuild = useMutation({
    mutationFn: () => api(`/api/admin/panels/${id}/astra-rebuild`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["astra-health", id] });
      flash(true, "نود بازسازی شد");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const toggle = useMutation({
    mutationFn: ({ oid, is_enabled }: { oid: number; is_enabled: boolean }) =>
      api<AstraOutbound>(`/api/admin/panels/${id}/outbounds/${oid}`, {
        method: "PATCH",
        body: JSON.stringify({ is_enabled }),
      }),
    onSuccess: (row) => {
      qc.setQueryData<AstraOutbound[]>(["astra-outbounds", id], (cur) =>
        (cur || []).map((r) => (r.id === row.id ? row : r))
      );
    },
  });

  const remove = useMutation({
    mutationFn: (oid: number) =>
      api(`/api/admin/panels/${id}/outbounds/${oid}`, { method: "DELETE" }),
    onSuccess: (_d, oid) => {
      setConfirmId(null);
      qc.setQueryData<AstraOutbound[]>(["astra-outbounds", id], (cur) =>
        (cur || []).filter((r) => r.id !== oid)
      );
      flash(true, "اوتباند حذف شد — می‌توانید اوتباند جدید اضافه کنید");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const toggleClient = useMutation({
    mutationFn: ({ email, enable }: { email: string; enable: boolean }) =>
      api<AstraNodeClient>(
        `/api/admin/panels/${id}/clients/${encodeURIComponent(email)}`,
        { method: "PATCH", body: JSON.stringify({ enable }) }
      ),
    onSuccess: (row) => {
      qc.setQueryData<AstraNodeClient[]>(["astra-clients", id], (cur) =>
        (cur || []).map((c) => (c.email === row.email ? row : c))
      );
      flash(true, row.enable ? `${row.email} روشن شد` : `${row.email} خاموش شد`);
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const syncUsage = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; updated: number }>(
        `/api/admin/panels/${id}/clients/sync-usage`,
        { method: "POST" }
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["astra-clients", id] });
      flash(true, `مصرف همگام شد · ${toFaDigits(res.updated)} کاربر`);
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((r) => {
      if (filter === "ok" && r.last_ping_ok !== true) return false;
      if (filter === "down" && r.last_ping_ok !== false) return false;
      if (filter === "off" && r.is_enabled) return false;
      if (!needle) return true;
      const hay = `${r.remark} ${r.host} ${r.protocol} ${r.port} ${r.uri}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [rows, filter, q]);

  const filteredClients = useMemo(() => {
    const needle = cq.trim().toLowerCase();
    if (!needle) return clients;
    return clients.filter((c) => {
      const hay = [
        c.email,
        c.astra_username,
        c.group_name,
        c.uuid,
        c.user_status,
        c.subscription_url,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(needle);
    });
  }, [clients, cq]);

  const okCount = rows.filter((r) => r.last_ping_ok === true).length;
  const badCount = rows.filter((r) => r.last_ping_ok === false).length;
  const offCount = rows.filter((r) => !r.is_enabled).length;
  const clientsOn = clients.filter((c) => c.enable).length;
  const clientsOff = clients.filter((c) => !c.enable).length;
  const healthPct = rows.length ? Math.round((okCount / rows.length) * 100) : 0;
  const avgMs = useMemo(() => {
    const ok = rows.filter((r) => r.last_ping_ok && r.last_ping_ms != null);
    if (!ok.length) return null;
    return Math.round(ok.reduce((s, r) => s + (r.last_ping_ms || 0), 0) / ok.length);
  }, [rows]);

  if (!Number.isFinite(id) || id <= 0) return <Navigate to="/panels" replace />;
  if (panels.length && panel && panel.type !== "astra") {
    return <Navigate to="/panels" replace />;
  }

  function onAdd(e: FormEvent) {
    e.preventDefault();
    if (!text.trim() || hasOutbound) return;
    add.mutate();
  }

  async function onCopy(label: string, value: string) {
    const ok = await copyText(value);
    flash(ok, ok ? `${label} کپی شد` : "کپی ناموفق بود");
  }

  return (
    <div className="ob-page">
      <div className="ob-atmosphere" aria-hidden />

      <header className="ob-hero">
        <div className="ob-hero-copy">
          <Link to="/panels" className="ob-back">
            <ArrowRight size={14} />
            بازگشت به پنل‌ها
          </Link>
          <h1>{panel?.name || `پنل #${id}`}</h1>
          <p className="ob-hero-lead">
            اوتباند CDN، مصرف با ضریب گروه، و مدیریت کانفیگ‌های این نود.
          </p>
          <span className="ob-kicker">
            <Radio size={13} />
            Astra · یک اوتباند در هر پنل
          </span>
        </div>

        <div className="ob-hero-panel">
          <div className={`ob-ring ${health?.listen_open ? "live" : "dead"}`}>
            <svg viewBox="0 0 120 120" className="ob-ring-svg" aria-hidden>
              <circle cx="60" cy="60" r="52" className="ob-ring-track" />
              <circle
                cx="60"
                cy="60"
                r="52"
                className="ob-ring-val"
                style={{
                  strokeDasharray: `${(healthPct / 100) * 327} 327`,
                }}
              />
            </svg>
            <div className="ob-ring-label">
              <strong>{toFaDigits(healthPct)}٪</strong>
              <span>سالم</span>
            </div>
          </div>
          <div className="ob-hero-meta">
            <div>
              <span>نود</span>
              <b className={health?.listen_open ? "ok" : "warn"}>
                {health?.listen_open ? "آنلاین" : "آفلاین"}
              </b>
            </div>
            <div>
              <span>آدرس لینک</span>
              <b dir="ltr">
                {health ? `${health.public_host}:${health.public_port}` : "—"}
              </b>
            </div>
            <div>
              <span>DNS</span>
              <b className={health?.dns_ok === false ? "warn" : "ok"}>
                {health?.dns_ok === false ? "پروکسی / نامعتبر" : "مستقیم"}
              </b>
            </div>
            <div>
              <span>پروتکل</span>
              <b dir="ltr">{health?.inbound || "vless+ws"}</b>
            </div>
            <div>
              <span>میانگین پینگ</span>
              <b dir="ltr">{avgMs != null ? `${toFaDigits(avgMs)} ms` : "—"}</b>
            </div>
          </div>
          {health?.dns_ok === false && health.dns_note && (
            <p className="ob-dns-warn" role="alert">
              {health.dns_note}
            </p>
          )}
        </div>
      </header>

      <div className="ob-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "outbounds"}
          className={`ob-tab${tab === "outbounds" ? " on" : ""}`}
          onClick={() => setTab("outbounds")}
        >
          <Wifi size={15} />
          اوتباند
          <em>{toFaDigits(rows.length)}/۱</em>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "clients"}
          className={`ob-tab${tab === "clients" ? " on" : ""}`}
          onClick={() => setTab("clients")}
        >
          <Users size={15} />
          کانفیگ‌ها
          <em>{toFaDigits(clients.length || 0)}</em>
        </button>
      </div>

      <div className="ob-actions-bar">
        {tab === "outbounds" ? (
          <>
            <button
              type="button"
              className="ob-btn ghost"
              onClick={() => void refetch()}
              disabled={isFetching}
            >
              <RefreshCw size={15} className={isFetching ? "ob-spin" : undefined} />
              تازه‌سازی
            </button>
            <button
              type="button"
              className="ob-btn ghost"
              disabled={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              <Activity size={15} />
              بازسازی نود
            </button>
            <button
              type="button"
              className={`ob-btn accent${pingAll.isPending ? " pulse" : ""}`}
              disabled={pingAll.isPending || !rows.length}
              onClick={() => pingAll.mutate()}
            >
              <Zap size={15} />
              {pingAll.isPending ? "در حال تست…" : "تست Reality"}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="ob-btn ghost"
              onClick={() => void refetchClients()}
              disabled={clientsFetching}
            >
              <RefreshCw size={15} className={clientsFetching ? "ob-spin" : undefined} />
              تازه‌سازی
            </button>
            <button
              type="button"
              className="ob-btn ghost"
              disabled={syncUsage.isPending}
              onClick={() => syncUsage.mutate()}
            >
              <Gauge size={15} />
              {syncUsage.isPending ? "همگام‌سازی…" : "همگام مصرف"}
            </button>
            <button
              type="button"
              className="ob-btn ghost"
              disabled={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              <Activity size={15} />
              بازسازی نود
            </button>
          </>
        )}
      </div>

      {msg && (
        <div className={`ob-toast ${msg.ok ? "ok" : "err"}`} role="status">
          {msg.ok ? <CheckCircle2 size={17} /> : <XCircle size={17} />}
          <span>{msg.text}</span>
          <button type="button" className="ob-toast-x" onClick={() => setMsg(null)}>
            ×
          </button>
        </div>
      )}

      {tab === "outbounds" && (
        <>
          <section className="ob-stats" aria-label="خلاصه">
            <article className={`ob-stat ${health?.listen_open ? "tone-ok" : "tone-warn"}`}>
              <div className="ob-stat-ico">
                <Server size={18} />
              </div>
              <div>
                <span>وضعیت نود</span>
                <strong>{health?.listen_open ? "فعال" : "پورت بسته"}</strong>
              </div>
            </article>
            <article className={`ob-stat ${okCount ? "tone-ok" : ""}`}>
              <div className="ob-stat-ico">
                <Wifi size={18} />
              </div>
              <div>
                <span>اوتباند</span>
                <strong>
                  {hasOutbound
                    ? okCount
                      ? "سالم"
                      : badCount
                        ? "قطع"
                        : offCount
                          ? "خاموش"
                          : "تنظیم‌شده"
                    : "خالی"}
                </strong>
              </div>
            </article>
            <article className="ob-stat">
              <div className="ob-stat-ico">
                <Users size={18} />
              </div>
              <div>
                <span>کانفیگ فعال</span>
                <strong>{toFaDigits(clientsOn)}</strong>
              </div>
            </article>
            <article className="ob-stat">
              <div className="ob-stat-ico">
                <Gauge size={18} />
              </div>
              <div>
                <span>پینگ</span>
                <strong dir="ltr">
                  {avgMs != null ? `${toFaDigits(avgMs)} ms` : "—"}
                </strong>
              </div>
            </article>
          </section>

          {!hasOutbound && (
            <section className="ob-compose">
              <div className="ob-compose-head">
                <div className="ob-compose-title">
                  <span className="ob-compose-badge">
                    <ClipboardPaste size={15} />
                  </span>
                  <div>
                    <h2>افزودن اوتباند</h2>
                    <p>فقط یک لینک share برای این پنل — بعد از افزودن، تست خودکار زده می‌شود</p>
                  </div>
                </div>
                <span className="ob-compose-count">ظرفیت ۱/۱</span>
              </div>
              <form onSubmit={onAdd} className="ob-compose-form">
                <textarea
                  className="ob-textarea"
                  rows={3}
                  dir="ltr"
                  placeholder="vless://uuid@host:443?encryption=none&security=reality&pbk=...&sid=...&sni=...#Out-DE"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                />
                <div className="ob-compose-foot">
                  <p>vless · vmess · trojan · ss — هر پنل Astra فقط یک اوتباند</p>
                  <button
                    type="submit"
                    className="ob-btn accent"
                    disabled={add.isPending || !text.trim()}
                  >
                    {add.isPending ? "در حال افزودن…" : "افزودن اوتباند"}
                  </button>
                </div>
              </form>
            </section>
          )}

          {hasOutbound && (
            <p className="ob-slot-note">
              این پنل اوتباند دارد. برای تعویض، اوتباند فعلی را حذف کنید و لینک جدید اضافه کنید.
            </p>
          )}

          <section className="ob-list-wrap">
            {rows.length > 1 && (
              <div className="ob-list-toolbar">
                <div className="ob-filters" role="tablist">
                  {(
                    [
                      ["all", "همه", rows.length],
                      ["ok", "سالم", okCount],
                      ["down", "قطع", badCount],
                      ["off", "خاموش", offCount],
                    ] as const
                  ).map(([k, label, n]) => (
                    <button
                      key={k}
                      type="button"
                      role="tab"
                      aria-selected={filter === k}
                      className={`ob-filter${filter === k ? " on" : ""}`}
                      onClick={() => setFilter(k)}
                    >
                      {label}
                      <em>{toFaDigits(n)}</em>
                    </button>
                  ))}
                </div>
                <label className="ob-search">
                  <Search size={15} />
                  <input
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="جستجو…"
                  />
                </label>
              </div>
            )}

            {isLoading && (
              <div className="ob-skeleton-grid">
                {Array.from({ length: 1 }).map((_, i) => (
                  <div key={i} className="ob-skeleton" />
                ))}
              </div>
            )}

            {!isLoading && !rows.length && (
              <div className="ob-empty-card">
                <div className="ob-empty-ico">
                  <Wifi size={26} />
                </div>
                <strong>اوتباندی تنظیم نشده</strong>
                <p>یک لینک share در باکس بالا پیست کنید تا کانفیگ‌ها روی این نود ساخته شوند.</p>
              </div>
            )}

            {!isLoading && rows.length > 0 && !filtered.length && (
              <div className="ob-empty-card soft">
                <strong>نتیجه‌ای نیست</strong>
                <p>فیلتر یا جستجو را عوض کنید.</p>
              </div>
            )}

            <div className={`ob-grid${rows.length === 1 ? " ob-grid-single" : ""}`}>
              {filtered.map((r, idx) => {
                const sec = securityOf(r.uri);
                const busy = pingingId === r.id;
                const tone =
                  r.last_ping_ok === true
                    ? latencyTone(r.last_ping_ms)
                    : r.last_ping_ok === false
                      ? "dead"
                      : "none";
                const pct =
                  r.last_ping_ok === true
                    ? latencyPct(r.last_ping_ms)
                    : r.last_ping_ok === false
                      ? 6
                      : 0;

                return (
                  <article
                    key={r.id}
                    className={`ob-card tone-${tone}${!r.is_enabled ? " is-off" : ""}`}
                    style={{ animationDelay: `${Math.min(idx, 12) * 40}ms` }}
                  >
                    <div className="ob-card-glow" aria-hidden />
                    <div className="ob-card-head">
                      <div className="ob-card-name">
                        <strong title={r.remark}>{r.remark || `Outbound #${r.id}`}</strong>
                        <div className="ob-tags">
                          <span className="ob-tag" dir="ltr">
                            {r.protocol}
                          </span>
                          <span className={`ob-tag sec-${sec}`} dir="ltr">
                            {sec}
                          </span>
                          {!r.is_enabled && <span className="ob-tag dim">خاموش</span>}
                        </div>
                      </div>
                      <div className={`ob-status-dot ${tone}`} title="وضعیت آخرین تست" />
                    </div>

                    <p className="ob-endpoint" dir="ltr">
                      {r.host}
                      <span>:{r.port}</span>
                    </p>

                    <div className="ob-latency">
                      <div className="ob-latency-top">
                        <span>
                          {busy
                            ? "در حال تست…"
                            : r.last_ping_ok === true
                              ? `${toFaDigits(r.last_ping_ms ?? 0)} ms`
                              : r.last_ping_ok === false
                                ? "Reality ناموفق"
                                : "تست نشده"}
                        </span>
                        <small>
                          {tone === "fast"
                            ? "عالی"
                            : tone === "mid"
                              ? "خوب"
                              : tone === "slow"
                                ? "کند"
                                : tone === "dead"
                                  ? "قطع"
                                  : "—"}
                        </small>
                      </div>
                      <div className="ob-latency-track">
                        <i
                          className={busy ? "busy" : undefined}
                          style={{ width: busy ? "55%" : `${pct}%` }}
                        />
                      </div>
                    </div>

                    <div className="ob-card-actions">
                      <button
                        type="button"
                        className="ob-mini accent"
                        disabled={busy || pingAll.isPending}
                        onClick={() => pingOne.mutate(r.id)}
                      >
                        <Shield size={14} />
                        {busy ? "…" : "تست"}
                      </button>
                      <button
                        type="button"
                        className="ob-mini"
                        onClick={() =>
                          void onCopy("لینک اوتباند", r.uri)
                        }
                      >
                        <Copy size={14} />
                        کپی
                      </button>
                      <button
                        type="button"
                        className="ob-mini"
                        onClick={() =>
                          toggle.mutate({ oid: r.id, is_enabled: !r.is_enabled })
                        }
                      >
                        <Power size={14} />
                        {r.is_enabled ? "خاموش" : "روشن"}
                      </button>
                      <button
                        type="button"
                        className="ob-mini danger"
                        onClick={() => setConfirmId(r.id)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        </>
      )}

      {tab === "clients" && (
        <>
          <section className="ob-stats ob-stats-4" aria-label="خلاصه کانفیگ‌ها">
            <article className="ob-stat">
              <div className="ob-stat-ico">
                <Users size={18} />
              </div>
              <div>
                <span>کل کانفیگ</span>
                <strong>{toFaDigits(clients.length)}</strong>
              </div>
            </article>
            <article className="ob-stat tone-ok">
              <div className="ob-stat-ico">
                <Power size={18} />
              </div>
              <div>
                <span>روشن</span>
                <strong>{toFaDigits(clientsOn)}</strong>
              </div>
            </article>
            <article className={`ob-stat ${clientsOff ? "tone-warn" : ""}`}>
              <div className="ob-stat-ico">
                <Power size={18} />
              </div>
              <div>
                <span>خاموش</span>
                <strong>{toFaDigits(clientsOff)}</strong>
              </div>
            </article>
            <article className={`ob-stat ${health?.listen_open ? "tone-ok" : "tone-warn"}`}>
              <div className="ob-stat-ico">
                <Server size={18} />
              </div>
              <div>
                <span>نود</span>
                <strong>{health?.listen_open ? "فعال" : "آفلاین"}</strong>
              </div>
            </article>
          </section>

          <section className="ob-list-wrap">
            <div className="ob-list-toolbar">
              <p className="ob-clients-hint">
                هر کانفیگ متعلق به یک اشتراک است · مصرف خام × ضریب گروه = مصرف Astra
              </p>
              <label className="ob-search">
                <Search size={15} />
                <input
                  value={cq}
                  onChange={(e) => setCq(e.target.value)}
                  placeholder="جستجو اشتراک، گروه، ایمیل…"
                />
              </label>
            </div>

            {clientsLoading && (
              <div className="ob-skeleton-grid">
                {Array.from({ length: 3 }).map((_, i) => (
                  <div key={i} className="ob-skeleton" />
                ))}
              </div>
            )}

            {!clientsLoading && !clients.length && (
              <div className="ob-empty-card">
                <div className="ob-empty-ico">
                  <Users size={26} />
                </div>
                <strong>هنوز کانفیگی روی این پنل نیست</strong>
                <p>
                  وقتی کاربر به گروهی وصل شود که این پنل Astra را دارد، اکانت اینجا ساخته می‌شود.
                </p>
              </div>
            )}

            {!clientsLoading && clients.length > 0 && !filteredClients.length && (
              <div className="ob-empty-card soft">
                <strong>نتیجه‌ای نیست</strong>
                <p>جستجو را عوض کنید.</p>
              </div>
            )}

            <div className="ob-clients">
              {filteredClients.map((c, idx) => {
                const pct = usagePct(c.weighted_used || c.used_traffic, c.data_limit);
                const capped =
                  c.data_limit > 0 && (c.weighted_used || c.used_traffic) >= c.data_limit;
                const subName = c.astra_username || c.email;
                const share = (c.share_links && c.share_links[0]) || "";
                const subUrl = c.subscription_url || "";
                const subShown = subOpenId === c.id;

                return (
                  <article
                    key={c.id}
                    className={`ob-client${c.enable ? "" : " is-off"}${capped ? " is-capped" : ""}`}
                    style={{ animationDelay: `${Math.min(idx, 12) * 35}ms` }}
                  >
                    <div className="ob-client-head">
                      <div className="ob-client-identity">
                        <span className="ob-client-sub-label">اشتراک</span>
                        <strong className="ob-client-sub-name" dir="ltr" title={subName}>
                          {subName}
                        </strong>
                        <div className="ob-tags">
                          {c.group_name && (
                            <span className="ob-tag">{c.group_name}</span>
                          )}
                          <span className="ob-tag dim">
                            ضریب {toFaDigits(String(c.coefficient))}
                          </span>
                          {c.limit_ip > 0 && (
                            <span className="ob-tag">IP ≤ {toFaDigits(c.limit_ip)}</span>
                          )}
                          {!c.enable && <span className="ob-tag dim">خاموش</span>}
                          {c.user_status && (
                            <span className={`ob-tag st-${c.user_status}`}>
                              {statusLabel(c.user_status)}
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="ob-client-head-actions">
                        <button
                          type="button"
                          className={`ob-mini${c.enable ? "" : " accent"}`}
                          disabled={toggleClient.isPending}
                          onClick={() =>
                            toggleClient.mutate({ email: c.email, enable: !c.enable })
                          }
                        >
                          <Power size={14} />
                          {c.enable ? "خاموش" : "روشن"}
                        </button>
                      </div>
                    </div>

                    <div className="ob-client-usage">
                      <div className="ob-latency-top">
                        <span>
                          خام {fmtTraffic(c.used_traffic)}
                          {c.coefficient !== 1 ? (
                            <> · با ضریب {fmtTraffic(c.weighted_used)}</>
                          ) : null}
                        </span>
                        <small>
                          سقف {c.data_limit > 0 ? fmtTraffic(c.data_limit) : "∞"}
                        </small>
                      </div>
                      <div className="ob-latency-track">
                        <i style={{ width: `${pct || (c.used_traffic ? 4 : 0)}%` }} />
                      </div>
                    </div>

                    <div className="ob-client-meta">
                      <span dir="ltr" className="ob-client-email" title={c.email}>
                        {c.email}
                      </span>
                      <span>{toFaDigits(c.links_count || (share ? 1 : 0))} لینک</span>
                    </div>

                    <div className="ob-client-actions">
                      <button
                        type="button"
                        className="ob-mini accent"
                        disabled={!share}
                        title={share ? "کپی کانفیگ این نود" : "لینک کانفیگ در دسترس نیست"}
                        onClick={() => void onCopy("کانفیگ", share)}
                      >
                        <Copy size={14} />
                        کپی کانفیگ
                      </button>
                      <button
                        type="button"
                        className="ob-mini"
                        disabled={!subUrl}
                        onClick={() =>
                          setSubOpenId((cur) => (cur === c.id ? null : c.id))
                        }
                      >
                        {subShown ? <EyeOff size={14} /> : <Eye size={14} />}
                        {subShown ? "بستن ساب" : "مشاهده ساب"}
                      </button>
                      <button
                        type="button"
                        className="ob-mini"
                        disabled={!subUrl}
                        onClick={() => void onCopy("سابسکریپشن", subUrl)}
                      >
                        <Copy size={14} />
                        کپی ساب
                      </button>
                      {subUrl && (
                        <a
                          className="ob-mini"
                          href={subUrl}
                          target="_blank"
                          rel="noreferrer"
                          title="باز کردن ساب"
                        >
                          <ExternalLink size={14} />
                          باز کردن
                        </a>
                      )}
                    </div>

                    {subShown && subUrl && (
                      <div className="ob-sub-panel">
                        <span className="ob-sub-panel-label">لینک اشتراک</span>
                        <code dir="ltr" className="ob-sub-url">
                          {subUrl}
                        </code>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          </section>
        </>
      )}

      {confirmId != null && (
        <div className="modal-backdrop" onClick={() => !remove.isPending && setConfirmId(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>حذف اوتباند</h3>
                <p className="modal-sub">از ساب‌های بعدی حذف می‌شود؛ بعد می‌توانید اوتباند جدید بگذارید</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmId(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              اوتباند{" "}
              <strong>
                {rows.find((r) => r.id === confirmId)?.remark || `#${confirmId}`}
              </strong>{" "}
              حذف شود؟
            </p>
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={remove.isPending}
                onClick={() => setConfirmId(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirmId)}
              >
                {remove.isPending ? "…" : "حذف"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
