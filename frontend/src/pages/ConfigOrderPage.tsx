import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  Loader2,
  RefreshCw,
  Save,
  ListOrdered,
} from "lucide-react";
import { ApiError, api } from "../lib/api";
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
};

type Workspace = {
  items: OrderItem[];
  sample_ready: boolean;
  total_sample_links: number;
  ordered_count: number;
};

function itemKey(it: OrderItem): string {
  return `${it.host}|${it.port}|${it.match_host || ""}`;
}

function endpointLabel(it: OrderItem): string {
  const base = `${it.host}:${it.port}`;
  return it.match_host ? `${base} · ${it.match_host}` : base;
}

export default function ConfigOrderPage() {
  const qc = useQueryClient();
  const [items, setItems] = useState<OrderItem[]>([]);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const dragIndex = useRef<number | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);

  const { data, isLoading, error, isFetching } = useQuery({
    queryKey: ["config-order"],
    queryFn: () => api<Workspace>("/api/admin/config-order"),
  });

  useEffect(() => {
    if (!data || dirty) return;
    setItems(data.items || []);
  }, [data, dirty]);

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
      setMsg({ type: "ok", text: "ترتیب ذخیره شد و روی همه ساب‌ها اعمال می‌شود" });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : "ذخیره ناموفق بود",
      });
    },
  });

  const buildSamples = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; workspace: Workspace }>("/api/admin/config-order/sample", {
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
      setMsg({
        type: "ok",
        text: "کانفیگ‌های نمونه از همه پنل‌ها ساخته/تازه‌سازی شد — حالا ترتیب را بچینید",
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : "ساخت نمونه ناموفق بود",
      });
    },
  });

  const busy = save.isPending || buildSamples.isPending || isFetching;

  function move(from: number, to: number) {
    if (to < 0 || to >= items.length || from === to) return;
    setItems((prev) => {
      const next = [...prev];
      const [row] = next.splice(from, 1);
      next.splice(to, 0, row);
      return next.map((it, i) => ({ ...it, sort_order: i }));
    });
    setDirty(true);
    setMsg(null);
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

  return (
    <div className="admin-page config-order-page">
      <header className="config-order-hero">
        <div>
          <p className="configs-kicker">سابسکریپشن</p>
          <h1 className="page-title">ترتیب کانفیگ‌ها</h1>
          <p className="page-sub">
            اول با «ساخت کانفیگ‌ها» نمونه را از همه پنل‌ها بگیرید، بعد با کشیدن یا
            دکمه‌های بالا/پایین ترتیب را بچینید. تطبیق بر اساس دامنه، پورت و هاست
            هدر/SNI است. اگر کاربری بعضی پنل‌ها را نداشت، همان کانفیگ‌ها در سابش
            نمی‌آید — فقط ترتیب بقیه اعمال می‌شود.
          </p>
        </div>
        <div className="config-order-hero-actions">
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => {
              setMsg(null);
              buildSamples.mutate();
            }}
          >
            {buildSamples.isPending ? (
              <Loader2 size={16} className="configs-spin" />
            ) : (
              <RefreshCw size={16} />
            )}
            <span>
              {buildSamples.isPending
                ? "در حال ساخت روی پنل‌ها…"
                : sampleReady
                  ? "ساخت مجدد کانفیگ‌ها"
                  : "ساخت کانفیگ‌ها"}
            </span>
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={busy || !dirty || !items.length}
            onClick={() => {
              setMsg(null);
              save.mutate(items);
            }}
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

      {msg && (
        <p className={`config-order-toast ${msg.type}`} role="status">
          {msg.text}
        </p>
      )}

      <div className="config-order-meta">
        <span>
          <ListOrdered size={14} aria-hidden />
          {toFaDigits(items.length)} کانفیگ یکتا
        </span>
        <span>{toFaDigits(totalSample)} از نمونه پنل‌ها</span>
        {missingCount > 0 ? (
          <span className="warn">{toFaDigits(missingCount)} فعلاً در نمونه نیست</span>
        ) : null}
        {dirty ? <span className="dirty">تغییرات ذخیره‌نشده</span> : null}
      </div>

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
            دکمه «ساخت کانفیگ‌ها» را بزنید تا از همه پنل‌های گروه‌ها نمونه ساخته شود.
          </p>
        </div>
      ) : (
        <ol className="config-order-list">
          {items.map((it, index) => (
            <li
              key={itemKey(it)}
              className={`config-order-row${overIndex === index ? " drag-over" : ""}${
                !it.in_sample ? " missing" : ""
              }`}
              draggable
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
              <span className="config-order-num">{toFaDigits(index + 1)}</span>
              <div className="config-order-body">
                <div className="config-order-title">
                  <strong>{it.label || endpointLabel(it)}</strong>
                  {it.protocol ? (
                    <span className="badge muted">{it.protocol}</span>
                  ) : null}
                  {!it.in_sample ? (
                    <span className="badge warn">فعلاً در نمونه‌ها نیست</span>
                  ) : null}
                </div>
                <div className="config-order-ep" dir="ltr">
                  {endpointLabel(it)}
                </div>
                {(it.panel_names.length > 0 || it.group_names.length > 0) && (
                  <div className="config-order-src">
                    {it.panel_names.length
                      ? `پنل: ${it.panel_names.join("، ")}`
                      : null}
                    {it.panel_names.length && it.group_names.length ? " · " : null}
                    {it.group_names.length
                      ? `گروه: ${it.group_names.join("، ")}`
                      : null}
                  </div>
                )}
              </div>
              <div className="config-order-move">
                <button
                  type="button"
                  className="btn ghost sm icon"
                  disabled={busy || index === 0}
                  onClick={() => move(index, index - 1)}
                  aria-label="بالاتر"
                  title="بالاتر"
                >
                  <ArrowUp size={15} />
                </button>
                <button
                  type="button"
                  className="btn ghost sm icon"
                  disabled={busy || index >= items.length - 1}
                  onClick={() => move(index, index + 1)}
                  aria-label="پایین‌تر"
                  title="پایین‌تر"
                >
                  <ArrowDown size={15} />
                </button>
              </div>
            </li>
          ))}
        </ol>
      )}

      {items.length > 0 && (
        <footer className="config-order-foot">
          <button
            type="button"
            className="btn"
            disabled={busy || !dirty}
            onClick={() => {
              setMsg(null);
              save.mutate(items);
            }}
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
