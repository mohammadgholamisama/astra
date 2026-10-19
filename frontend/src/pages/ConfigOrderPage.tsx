import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  Eye,
  EyeOff,
  GripVertical,
  Loader2,
  RefreshCw,
  Save,
  Search,
  ListOrdered,
  X,
} from "lucide-react";
import { ToastStack, useToastStack } from "../components/ToastStack";
import { ApiError, api } from "../lib/api";
import { countriesForPicker, countryName } from "../lib/countries";
import { toFaDigits } from "../lib/format";

type OrderItem = {
  host: string;
  port: number;
  match_host: string;
  label: string | null;
  sort_order: number;
  in_sample: boolean;
  sample_link: string | null;
  protocol: string | null;
  panel_names: string[];
  group_names: string[];
  hidden: boolean;
  country_code: string | null;
  flag: string | null;
  flag_exact: boolean;
};

type Workspace = {
  items: OrderItem[];
  sample_ready: boolean;
  total_sample_links: number;
  ordered_count: number;
};

function flagEmoji(code: string): string {
  const c = (code || "").trim().toUpperCase();
  if (c.length !== 2) return "";
  return String.fromCodePoint(
    ...[...c].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65)
  );
}

/** Real flag image (Windows often shows FR/DE letters for emoji flags). */
function FlagImg({
  code,
  size = 28,
  className,
}: {
  code: string;
  size?: number;
  className?: string;
}) {
  const c = (code || "").trim().toLowerCase();
  if (c.length !== 2) return null;
  const h = Math.round((size * 3) / 4);
  return (
    <img
      className={className || "config-order-flag-img"}
      src={`https://flagcdn.com/w80/${c}.png`}
      srcSet={`https://flagcdn.com/w40/${c}.png 1x, https://flagcdn.com/w80/${c}.png 2x`}
      alt=""
      width={size}
      height={h}
      loading="lazy"
      decoding="async"
      draggable={false}
    />
  );
}

function itemKey(it: Pick<OrderItem, "host" | "port" | "match_host">): string {
  return `${it.host}|${it.port}|${it.match_host || ""}`;
}

function endpointLabel(it: Pick<OrderItem, "host" | "port" | "match_host">): string {
  const base = `${it.host}:${it.port}`;
  return it.match_host ? `${base} · ${it.match_host}` : base;
}

function patchItem(
  list: OrderItem[],
  key: string,
  patch: Partial<OrderItem>
): OrderItem[] {
  return list.map((it) => (itemKey(it) === key ? { ...it, ...patch } : it));
}

export default function ConfigOrderPage() {
  const qc = useQueryClient();
  const { toasts, push: pushToast, dismiss: dismissToast } = useToastStack();
  const [items, setItems] = useState<OrderItem[]>([]);
  const [dirty, setDirty] = useState(false);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "visible" | "hidden">("all");
  const [flagRow, setFlagRow] = useState<OrderItem | null>(null);
  const [flagQ, setFlagQ] = useState("");
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const flagPopRef = useRef<HTMLDivElement | null>(null);
  const dragIndex = useRef<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["config-order"],
    queryFn: () => api<Workspace>("/api/admin/config-order"),
  });

  useEffect(() => {
    if (!data || dirty) return;
    setItems(data.items || []);
  }, [data, dirty]);

  useEffect(() => {
    if (!flagRow) return;
    setFlagQ("");
    function onDoc(e: MouseEvent) {
      const el = flagPopRef.current;
      if (el && !el.contains(e.target as Node)) setFlagRow(null);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setFlagRow(null);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [flagRow]);

  const flagChoices = useMemo(() => countriesForPicker(flagQ), [flagQ]);

  const sampleReady = !!data?.sample_ready;
  const totalSample = data?.total_sample_links ?? 0;

  const save = useMutation({
    mutationFn: (next: OrderItem[]) =>
      api<Workspace>("/api/admin/config-order", {
        method: "PUT",
        body: JSON.stringify({
          items: next.map((it) => ({
            host: it.host,
            port: it.port,
            match_host: it.match_host || "",
            label: it.label || null,
          })),
        }),
      }),
    onSuccess: (ws) => {
      setItems(ws.items || []);
      setDirty(false);
      qc.setQueryData(["config-order"], ws);
      pushToast("ok", "ترتیب ذخیره شد و روی همه ساب‌ها اعمال شد");
    },
    onError: (e) => {
      setDirty(true);
      pushToast(
        "err",
        e instanceof ApiError ? e.message : "ذخیره ترتیب ناموفق بود"
      );
    },
  });

  const buildSamples = useMutation({
    mutationFn: () =>
      api<{
        ok: boolean;
        workspace: Workspace;
        sample?: {
          ok_count?: number;
          fail_count?: number;
          panels_count?: number;
          targets_count?: number;
          groups_count?: number;
          links_count?: number;
        };
      }>("/api/admin/config-order/sample", {
        method: "POST",
      }),
    onSuccess: (res) => {
      const ws = res.workspace;
      if (ws) {
        setItems(ws.items || []);
        setDirty(false);
        qc.setQueryData(["config-order"], ws);
      } else {
        void qc.invalidateQueries({ queryKey: ["config-order"] });
      }
      const s = res.sample;
      const okN = s?.ok_count ?? 0;
      const failN = s?.fail_count ?? 0;
      const linkN =
        s?.links_count ?? ws?.total_sample_links ?? ws?.items?.length ?? 0;
      const gN = s?.groups_count;
      const base =
        failN > 0
          ? `تست قبلی حذف شد؛ ${toFaDigits(okN)} پنل موفق، ${toFaDigits(failN)} ناموفق — ${toFaDigits(linkN)} کانفیگ یکتا`
          : `مثل یوزر همه‌گروه ساخته شد — ${toFaDigits(okN)} پنل${
              gN != null ? `، ${toFaDigits(gN)} گروه` : ""
            }، ${toFaDigits(linkN)} کانفیگ یکتا`;
      pushToast(
        failN > 0 && okN === 0 ? "err" : "ok",
        `${base} — لیست به‌روز شد`
      );
    },
    onError: (e) => {
      pushToast(
        "err",
        e instanceof ApiError ? e.message : "ساخت نمونه ناموفق بود"
      );
    },
  });

  const toggleHide = useMutation({
    mutationFn: async (row: OrderItem) => {
      const key = itemKey(row);
      setPendingKey(key);
      const nextHidden = !row.hidden;
      await api("/api/admin/config-visibility", {
        method: "PUT",
        body: JSON.stringify({
          host: row.host,
          port: Number(row.port),
          match_host: (row.match_host || "").trim(),
          hidden: nextHidden,
        }),
      });
      return { key, hidden: nextHidden, row };
    },
    onSuccess: ({ key, hidden, row }) => {
      setItems((prev) => patchItem(prev, key, { hidden }));
      qc.setQueryData<Workspace>(["config-order"], (old) =>
        old
          ? { ...old, items: patchItem(old.items || [], key, { hidden }) }
          : old
      );
      const ep = endpointLabel(row);
      pushToast(
        "ok",
        hidden
          ? `${ep} از همه ساب‌ها مخفی شد`
          : `${ep} دوباره در همه ساب‌ها نمایش داده می‌شود`
      );
    },
    onError: (e) => {
      pushToast(
        "err",
        e instanceof ApiError ? e.message : "خطا در تغییر نمایش"
      );
    },
    onSettled: () => setPendingKey(null),
  });

  const setFlag = useMutation({
    mutationFn: async (args: {
      row: OrderItem;
      country_code: string | null;
    }) => {
      const { row, country_code } = args;
      const key = itemKey(row);
      setPendingKey(key);
      const res = await api<{
        ok: boolean;
        location: {
          country_code: string;
          flag: string;
        } | null;
      }>("/api/admin/config-locations/endpoint", {
        method: "PUT",
        body: JSON.stringify({
          host: row.host,
          port: Number(row.port),
          match_host: (row.match_host || "").trim(),
          country_code,
        }),
      });
      return { res, row, country_code, key };
    },
    onSuccess: ({ res, row, country_code, key }) => {
      const loc = res?.location;
      const patch: Partial<OrderItem> = loc
        ? {
            country_code: loc.country_code,
            flag: loc.flag || flagEmoji(loc.country_code),
            flag_exact: true,
          }
        : {
            country_code: null,
            flag: null,
            flag_exact: false,
          };
      setItems((prev) => patchItem(prev, key, patch));
      qc.setQueryData<Workspace>(["config-order"], (old) =>
        old
          ? { ...old, items: patchItem(old.items || [], key, patch) }
          : old
      );
      setFlagRow(null);
      const ep = endpointLabel(row);
      const name = countryName(country_code) || country_code;
      pushToast(
        "ok",
        country_code
          ? `پرچم ${name} برای ${ep} روی همه ساب‌ها ست شد`
          : `پرچم ${ep} برداشته شد`
      );
    },
    onError: (e) => {
      pushToast(
        "err",
        e instanceof ApiError ? e.message : "خطا در تنظیم پرچم"
      );
    },
    onSettled: () => setPendingKey(null),
  });

  const busy =
    save.isPending ||
    buildSamples.isPending ||
    toggleHide.isPending ||
    setFlag.isPending;

  function applyOrder(next: OrderItem[], { persist = true }: { persist?: boolean } = {}) {
    const ordered = next.map((it, i) => ({ ...it, sort_order: i }));
    setItems(ordered);
    setDirty(true);
    if (persist) {
      save.mutate(ordered);
    }
  }

  function move(from: number, to: number) {
    if (to < 0 || to >= items.length || from === to) return;
    const next = [...items];
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    applyOrder(next);
  }

  /** Move within the currently visible (filtered) list — not absolute neighbors. */
  function moveInFiltered(absIndex: number, direction: -1 | 1) {
    const positions = filtered.map((f) => f.index);
    const pos = positions.indexOf(absIndex);
    if (pos < 0) return;
    const targetPos = pos + direction;
    if (targetPos < 0 || targetPos >= positions.length) return;
    move(absIndex, positions[targetPos]);
  }

  /**
   * Jump by subscription #N (visible-only numbering on the page).
   * Item becomes that #; the previous #N and everything after shift down one.
   * Hidden rows between slots stay put relative to the list.
   */
  function moveToSubNumber(fromAbs: number, targetSub: number) {
    if (!Number.isFinite(targetSub) || targetSub < 1) return;
    const row = items[fromAbs];
    if (!row || row.hidden) return;

    const visibleAbs: number[] = [];
    items.forEach((it, i) => {
      if (!it.hidden) visibleAbs.push(i);
    });
    const fromPos = visibleAbs.indexOf(fromAbs);
    if (fromPos < 0) return;

    const toPos = Math.max(
      0,
      Math.min(visibleAbs.length - 1, Math.floor(targetSub) - 1)
    );
    if (fromPos === toPos) return;

    const next = [...items];
    const [moved] = next.splice(fromAbs, 1);

    // Insert so `moved` becomes the toPos-th visible item among `next`
    let seen = 0;
    let insertAt = next.length;
    for (let i = 0; i <= next.length; i++) {
      if (seen === toPos) {
        insertAt = i;
        break;
      }
      if (i < next.length && !next[i].hidden) seen += 1;
    }
    next.splice(insertAt, 0, moved);
    applyOrder(next);
  }

  function parseJumpNumber(raw: string): number | null {
    const digits = String(raw || "")
      .trim()
      .replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)))
      .replace(/[^\d]/g, "");
    if (!digits) return null;
    const n = Number.parseInt(digits, 10);
    return Number.isFinite(n) && n >= 1 ? n : null;
  }

  function onDragStart(index: number) {
    dragIndex.current = index;
  }

  function onDragOver(e: React.DragEvent, index: number) {
    e.preventDefault();
    setOverIndex(index);
  }

  function onDrop(index: number) {
    const from = dragIndex.current;
    dragIndex.current = null;
    setOverIndex(null);
    if (from == null) return;
    move(from, index);
  }

  const missingCount = useMemo(
    () => items.filter((it) => !it.in_sample).length,
    [items]
  );
  const hiddenCount = useMemo(
    () => items.filter((it) => it.hidden).length,
    [items]
  );

  /** Same #N as subscription page (visible configs only, global order). */
  const subNumByKey = useMemo(() => {
    const map = new Map<string, number>();
    let n = 0;
    for (const it of items) {
      if (it.hidden) continue;
      n += 1;
      map.set(itemKey(it), n);
    }
    return map;
  }, [items]);

  const filtered = useMemo(() => {
    const needleRaw = q.trim();
    const needle = needleRaw.toLowerCase();
    const needleDigits = needleRaw
      .replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)))
      .replace(/^#/, "")
      .trim();
    return items
      .map((it, index) => ({ it, index }))
      .filter(({ it }) => {
        if (filter === "visible" && it.hidden) return false;
        if (filter === "hidden" && !it.hidden) return false;
        if (!needle) return true;
        const subN = subNumByKey.get(itemKey(it));
        const hay = [
          it.host,
          String(it.port),
          it.match_host || "",
          it.label || "",
          it.protocol || "",
          ...(it.panel_names || []),
          ...(it.group_names || []),
          subN != null ? String(subN) : "",
          subN != null ? `#${subN}` : "",
          subN != null ? toFaDigits(subN) : "",
          subN != null ? `#${toFaDigits(subN)}` : "",
        ]
          .join(" ")
          .toLowerCase();
        if (hay.includes(needle)) return true;
        if (needleDigits && /^\d+$/.test(needleDigits) && subN != null) {
          return String(subN) === needleDigits;
        }
        return false;
      });
  }, [items, q, filter, subNumByKey]);

  return (
    <div className="admin-page config-order-page">
      <ToastStack toasts={toasts} onDismiss={dismissToast} />

      <header className="config-order-hero">
        <div>
          <p className="configs-kicker">سابسکریپشن</p>
          <h1 className="page-title">ترتیب کانفیگ‌ها</h1>
          <p className="page-sub">
            شمارهٔ <strong>#</strong> همان شمارهٔ صفحهٔ ساب است (فقط کانفیگ‌های
            نمایش‌داده‌شده). هر ردیف با <strong>دامنه</strong>، <strong>پورت</strong> و در
            صورت وجود <strong>هاست (SNI)</strong> مشخص می‌شود. پرچم و خاموش/روشن روی
            همه ساب‌ها اعمال می‌شود.
          </p>
        </div>
        <div className="config-order-hero-actions">
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => buildSamples.mutate()}
          >
            {buildSamples.isPending ? (
              <Loader2 size={16} className="configs-spin" />
            ) : (
              <RefreshCw size={16} />
            )}
            <span>
              {buildSamples.isPending
                ? "در حال ساخت روی همه پنل‌ها…"
                : sampleReady
                  ? "ساخت مجدد کانفیگ‌ها"
                  : "ساخت کانفیگ‌ها"}
            </span>
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={busy || !dirty || !items.length}
            onClick={() => save.mutate(items)}
          >
            {save.isPending ? (
              <Loader2 size={16} className="configs-spin" />
            ) : (
              <Save size={16} />
            )}
            <span>{save.isPending ? "در حال ذخیره…" : "ذخیره ترتیب"}</span>
          </button>
        </div>
      </header>

      <div className="config-order-meta">
        <span>
          <ListOrdered size={14} aria-hidden />
          {toFaDigits(items.length)} کانفیگ یکتا
        </span>
        <span>{toFaDigits(totalSample)} از نمونه پنل‌ها</span>
        {hiddenCount > 0 ? (
          <span className="warn">
            {toFaDigits(hiddenCount)} مخفی از ساب
          </span>
        ) : null}
        {missingCount > 0 ? (
          <span className="warn">
            {toFaDigits(missingCount)} فعلاً در نمونه نیست
          </span>
        ) : null}
        {dirty ? <span className="dirty">تغییرات ذخیره‌نشده</span> : null}
      </div>

      {items.length > 0 ? (
        <div className="config-order-toolbar">
          <label className="config-order-search">
            <Search size={15} aria-hidden />
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="جستجو: #۲۵، دامنه، پورت، پنل…"
            />
          </label>
          <div className="configs-filters" role="group" aria-label="فیلتر نمایش">
            {(
              [
                ["all", "همه"],
                ["visible", "نمایش"],
                ["hidden", "مخفی"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={filter === key ? "active" : ""}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {isLoading ? (
        <p className="muted">در حال بارگذاری…</p>
      ) : error ? (
        <p className="error">
          {error instanceof Error ? error.message : "خطا در بارگذاری"}
        </p>
      ) : items.length === 0 ? (
        <div className="config-order-empty">
          <p>هنوز کانفیگی برای چیدن نیست.</p>
          <p className="muted">
            دکمه «ساخت کانفیگ‌ها» را بزنید تا روی همه پنل‌های فعال نمونه ساخته شود.
          </p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="config-order-empty">
          <p>با این فیلتر چیزی پیدا نشد</p>
        </div>
      ) : (
        <ol className="config-order-list">
          {filtered.map(({ it, index }, fi) => {
            const key = itemKey(it);
            const flagOpen =
              flagRow != null && itemKey(flagRow) === key;
            const hideBusy = pendingKey === key;
            const shown = !it.hidden;
            const subN = subNumByKey.get(key);
            const canJump = !busy && !it.hidden && subN != null;
            return (
              <li
                key={key}
                className={`config-order-row${
                  overIndex === index ? " drag-over" : ""
                }${!it.in_sample ? " missing" : ""}${
                  it.hidden ? " is-hidden" : ""
                }`}
                draggable={!flagOpen && !busy}
                onDragStart={() => onDragStart(index)}
                onDragOver={(e) => onDragOver(e, index)}
                onDrop={() => onDrop(index)}
                onDragEnd={() => {
                  dragIndex.current = null;
                  setOverIndex(null);
                }}
              >
                <span className="config-order-grip" aria-hidden>
                  <GripVertical size={16} />
                </span>
                <span
                  className={`config-order-num${
                    it.hidden ? " is-hidden-num" : ""
                  }`}
                  title={
                    it.hidden
                      ? "مخفی از ساب — شماره‌ای در ساب ندارد"
                      : `شماره در ساب: #${toFaDigits(subN ?? 0)}`
                  }
                >
                  {it.hidden
                    ? "—"
                    : `#${toFaDigits(subN ?? 0)}`}
                </span>
                <div className="config-order-body">
                  <div className="config-order-title">
                    {it.label ? (
                      <strong className="config-order-label">{it.label}</strong>
                    ) : null}
                    {it.protocol ? (
                      <span className="badge muted">{it.protocol}</span>
                    ) : null}
                    {it.hidden ? (
                      <span className="badge warn">مخفی از ساب</span>
                    ) : (
                      <span className="badge ok">در ساب</span>
                    )}
                    {!it.in_sample ? (
                      <span className="badge warn">دیگر در نمونه نیست</span>
                    ) : null}
                  </div>
                  <div className="config-order-fp" dir="ltr">
                    <div className="config-order-fp-cell">
                      <span className="config-order-fp-k">دامنه</span>
                      <span className="config-order-fp-v" title={it.host}>
                        {it.host}
                      </span>
                    </div>
                    <div className="config-order-fp-cell port">
                      <span className="config-order-fp-k">پورت</span>
                      <span className="config-order-fp-v">
                        {toFaDigits(it.port)}
                      </span>
                    </div>
                    <div
                      className={`config-order-fp-cell host${
                        it.match_host ? "" : " empty"
                      }`}
                    >
                      <span className="config-order-fp-k">هاست / SNI</span>
                      <span
                        className="config-order-fp-v"
                        title={it.match_host || undefined}
                      >
                        {it.match_host || "—"}
                      </span>
                    </div>
                  </div>
                  {(it.panel_names.length > 0 || it.group_names.length > 0) && (
                    <div className="config-order-src">
                      {it.panel_names.length ? (
                        <span>پنل: {it.panel_names.join("، ")}</span>
                      ) : null}
                      {it.group_names.length ? (
                        <span>گروه: {it.group_names.join("، ")}</span>
                      ) : null}
                    </div>
                  )}
                </div>

                <div className="config-order-actions">
                  <div
                    className="configs-flag-wrap"
                    ref={flagOpen ? flagPopRef : undefined}
                  >
                    <button
                      type="button"
                      className={`configs-flag-btn${
                        it.country_code ? " has-flag" : ""
                      }${flagOpen ? " open" : ""}`}
                      disabled={busy}
                      onClick={() => setFlagRow(flagOpen ? null : it)}
                      title={
                        it.country_code
                          ? `پرچم: ${countryName(it.country_code)}`
                          : "انتخاب پرچم"
                      }
                      aria-label={
                        it.country_code
                          ? `پرچم ${countryName(it.country_code)}`
                          : "انتخاب پرچم"
                      }
                      aria-expanded={flagOpen}
                    >
                      {it.country_code ? (
                        <FlagImg code={it.country_code} size={30} />
                      ) : (
                        <span className="config-order-flag-empty" aria-hidden>
                          🏳️
                        </span>
                      )}
                    </button>
                    {flagOpen ? (
                      <div
                        className="configs-flag-pop"
                        role="dialog"
                        aria-label="انتخاب کشور"
                      >
                        <div className="configs-flag-pop-head">
                          <span>پرچم برای ساب</span>
                          <button
                            type="button"
                            className="modal-close"
                            onClick={() => setFlagRow(null)}
                            aria-label="بستن"
                          >
                            <X size={16} />
                          </button>
                        </div>
                        <p className="configs-flag-ep" dir="ltr">
                          {endpointLabel(it)}
                        </p>
                        <label className="configs-flag-search">
                          <Search size={14} aria-hidden />
                          <input
                            type="search"
                            value={flagQ}
                            onChange={(e) => setFlagQ(e.target.value)}
                            placeholder="جستجوی کشور…"
                            autoFocus
                          />
                        </label>
                        <button
                          type="button"
                          className={`configs-flag-none${
                            !it.flag_exact ? " active" : ""
                          }`}
                          disabled={setFlag.isPending}
                          onClick={() =>
                            setFlag.mutate({
                              row: it,
                              country_code: null,
                            })
                          }
                        >
                          <span aria-hidden>🚫</span>
                          <span>بدون پرچم</span>
                        </button>
                        <div className="configs-flag-grid">
                          {flagChoices.map((c) => {
                            const active =
                              (it.country_code || "").toUpperCase() === c.code;
                            return (
                              <button
                                key={c.code}
                                type="button"
                                className={`configs-flag-opt${
                                  active ? " active" : ""
                                }`}
                                disabled={setFlag.isPending}
                                onClick={() =>
                                  setFlag.mutate({
                                    row: it,
                                    country_code: c.code,
                                  })
                                }
                                title={c.name}
                              >
                                <FlagImg code={c.code} size={28} />
                                <small>{c.name}</small>
                              </button>
                            );
                          })}
                        </div>
                        {flagChoices.length === 0 ? (
                          <p className="configs-flag-inherited muted">
                            کشوری با این جستجو پیدا نشد
                          </p>
                        ) : null}
                        {it.flag && !it.flag_exact ? (
                          <p className="configs-flag-inherited muted">
                            پرچم فعلی از قانون کلی دامنه:پورت می‌آید — برای همین
                            هاست یک پرچم جدا انتخاب کنید یا «بدون پرچم» بزنید.
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                  </div>

                  <label
                    className={`configs-switch${hideBusy ? " busy" : ""}`}
                    title={
                      shown
                        ? "الان در ساب هست — برای مخفی کردن بزنید"
                        : "مخفی است — برای نمایش بزنید"
                    }
                  >
                    <span className="configs-switch-label">
                      {shown ? (
                        <>
                          <Eye size={14} />
                          نمایش
                        </>
                      ) : (
                        <>
                          <EyeOff size={14} />
                          مخفی
                        </>
                      )}
                    </span>
                    <input
                      type="checkbox"
                      checked={shown}
                      disabled={busy}
                      onChange={() => toggleHide.mutate(it)}
                      aria-label={
                        shown
                          ? `مخفی کردن ${key} از ساب`
                          : `نمایش ${key} در ساب`
                      }
                    />
                    <span className="configs-switch-track" aria-hidden />
                  </label>

                  <div className="config-order-move">
                    <button
                      type="button"
                      className="btn ghost sm icon"
                      disabled={busy || fi === 0}
                      onClick={() => moveInFiltered(index, -1)}
                      aria-label="بالاتر"
                      title="بالاتر"
                    >
                      <ArrowUp size={15} />
                    </button>
                    <input
                      className="config-order-jump"
                      type="text"
                      inputMode="numeric"
                      enterKeyHint="done"
                      disabled={!canJump}
                      placeholder={subN != null ? toFaDigits(subN) : "—"}
                      aria-label="برو به شماره ساب"
                      title={
                        canJump
                          ? "شماره مقصد را بنویس و Enter بزن — جای همان # می‌رود و بقیه یکی پایین می‌آیند"
                          : "برای کانفیگ مخفی جابه‌جایی با شماره ممکن نیست"
                      }
                      onClick={(e) => e.stopPropagation()}
                      onMouseDown={(e) => e.stopPropagation()}
                      onFocus={(e) => e.currentTarget.select()}
                      onKeyDown={(e) => {
                        if (e.key !== "Enter") return;
                        e.preventDefault();
                        e.stopPropagation();
                        const n = parseJumpNumber(e.currentTarget.value);
                        if (n == null) return;
                        moveToSubNumber(index, n);
                        e.currentTarget.value = "";
                        e.currentTarget.blur();
                      }}
                    />
                    <button
                      type="button"
                      className="btn ghost sm icon"
                      disabled={busy || fi >= filtered.length - 1}
                      onClick={() => moveInFiltered(index, 1)}
                      aria-label="پایین‌تر"
                      title="پایین‌تر"
                    >
                      <ArrowDown size={15} />
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {items.length > 0 && (
        <footer className="config-order-foot">
          <button
            type="button"
            className="btn"
            disabled={busy || !dirty}
            onClick={() => save.mutate(items)}
          >
            {save.isPending ? (
              <Loader2 size={16} className="configs-spin" />
            ) : (
              <Save size={16} />
            )}
            <span>{save.isPending ? "در حال ذخیره…" : "ذخیره ترتیب"}</span>
          </button>
        </footer>
      )}
    </div>
  );
}
