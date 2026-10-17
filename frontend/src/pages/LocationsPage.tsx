import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MapPin, Pencil, Plus, Trash2, X } from "lucide-react";
import { ApiError, api } from "../lib/api";

export type ConfigLocation = {
  id: number;
  host: string;
  port: number;
  /** Optional link header/SNI host (host=… / sni=…) — empty = domain+port only */
  match_host?: string;
  country_code: string;
  flag: string;
};

const COUNTRIES: { code: string; name: string }[] = [
  { code: "DE", name: "آلمان" },
  { code: "TR", name: "ترکیه" },
  { code: "NL", name: "هلند" },
  { code: "US", name: "آمریکا" },
  { code: "GB", name: "انگلیس" },
  { code: "FR", name: "فرانسه" },
  { code: "AE", name: "امارات" },
  { code: "FI", name: "فنلاند" },
  { code: "SE", name: "سوئد" },
  { code: "NO", name: "نروژ" },
  { code: "PL", name: "لهستان" },
  { code: "AT", name: "اتریش" },
  { code: "CH", name: "سوئیس" },
  { code: "IT", name: "ایتالیا" },
  { code: "ES", name: "اسپانیا" },
  { code: "CA", name: "کانادا" },
  { code: "JP", name: "ژاپن" },
  { code: "SG", name: "سنگاپور" },
  { code: "HK", name: "هنگ‌کنگ" },
  { code: "KR", name: "کره جنوبی" },
  { code: "IN", name: "هند" },
  { code: "RU", name: "روسیه" },
  { code: "UA", name: "اوکراین" },
  { code: "CZ", name: "چک" },
  { code: "RO", name: "رومانی" },
  { code: "BG", name: "بلغارستان" },
  { code: "MD", name: "مولداوی" },
  { code: "LV", name: "لتونی" },
  { code: "LT", name: "لیتوانی" },
  { code: "EE", name: "استونی" },
  { code: "PT", name: "پرتغال" },
  { code: "BE", name: "بلژیک" },
  { code: "DK", name: "دانمارک" },
  { code: "IE", name: "ایرلند" },
  { code: "AU", name: "استرالیا" },
  { code: "BR", name: "برزیل" },
  { code: "IR", name: "ایران" },
  { code: "IQ", name: "عراق" },
  { code: "AM", name: "ارمنستان" },
  { code: "AZ", name: "آذربایجان" },
  { code: "GE", name: "گرجستان" },
  { code: "KZ", name: "قزاقستان" },
  { code: "CY", name: "قبرس" },
  { code: "GR", name: "یونان" },
  { code: "IL", name: "اسرائیل" },
  { code: "SA", name: "عربستان" },
  { code: "EG", name: "مصر" },
  { code: "ZA", name: "آفریقای جنوبی" },
  { code: "MX", name: "مکزیک" },
  { code: "AR", name: "آرژانتین" },
  { code: "CL", name: "شیلی" },
];

function flagEmoji(code: string): string {
  const c = (code || "").trim().toUpperCase();
  if (c.length !== 2) return "";
  return String.fromCodePoint(...[...c].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65));
}

type FormState = {
  host: string;
  port: string;
  match_host: string;
  country_code: string;
};

const emptyForm: FormState = { host: "", port: "", match_host: "", country_code: "DE" };

export default function LocationsPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ConfigLocation | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [confirm, setConfirm] = useState<ConfigLocation | null>(null);
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const { data: locations = [], isLoading, error } = useQuery({
    queryKey: ["config-locations"],
    queryFn: () => api<ConfigLocation[]>("/api/admin/config-locations"),
  });

  const previewFlag = useMemo(() => flagEmoji(form.country_code), [form.country_code]);
  const countryName = useMemo(() => {
    const hit = COUNTRIES.find((c) => c.code === form.country_code.toUpperCase());
    return hit?.name || form.country_code;
  }, [form.country_code]);

  function openCreate() {
    setEditing(null);
    setForm(emptyForm);
    setMsg(null);
    setOpen(true);
  }

  function openEdit(row: ConfigLocation) {
    setEditing(row);
    setForm({
      host: row.host,
      port: String(row.port),
      match_host: row.match_host || "",
      country_code: row.country_code,
    });
    setMsg(null);
    setOpen(true);
  }

  const save = useMutation({
    mutationFn: async () => {
      const host = form.host.trim();
      const port = Number(form.port);
      const match_host = form.match_host.trim() || null;
      const country_code = form.country_code.trim().toUpperCase();
      if (!host) throw new Error("دامنه را وارد کنید");
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error("پورت باید بین ۱ تا ۶۵۵۳۵ باشد");
      }
      if (country_code.length !== 2) throw new Error("کد کشور نامعتبر است");
      const body = { host, port, match_host, country_code, label: null };
      if (editing) {
        return api<ConfigLocation>(`/api/admin/config-locations/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      return api<ConfigLocation>("/api/admin/config-locations", {
        method: "POST",
        body: JSON.stringify(body),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config-locations"] });
      setOpen(false);
      setMsg({
        type: "ok",
        text: editing ? "لوکیشن به‌روز شد" : "لوکیشن ثبت شد",
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text: e instanceof ApiError || e instanceof Error ? e.message : "خطا در ذخیره",
      });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/admin/config-locations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config-locations"] });
      setConfirm(null);
      setMsg({ type: "ok", text: "لوکیشن حذف شد" });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text: e instanceof ApiError || e instanceof Error ? e.message : "خطا در حذف",
      });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }

  return (
    <div className="admin-page locations-page">
      <div className="locations-toolbar">
        <div>
          <h1 className="page-title">لوکیشن کانفیگ</h1>
          <p className="page-sub">
            دامنه و پورت (و در صورت نیاز هاست هدر/SNI) را به پرچم وصل کنید.
          </p>
        </div>
        <button type="button" className="btn" onClick={openCreate}>
          <Plus size={16} />
          لوکیشن جدید
        </button>
      </div>

      {msg && (
        <p className={msg.type === "ok" ? "ok" : "error"} role="status">
          {msg.text}
        </p>
      )}

      {error ? (
        <div className="admin-empty">
          <p>{error instanceof Error ? error.message : "خطا در بارگذاری"}</p>
        </div>
      ) : isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : locations.length === 0 ? (
        <div className="admin-empty">
          <MapPin size={36} strokeWidth={1.4} />
          <p>هنوز لوکیشنی تعریف نشده</p>
          <button type="button" className="btn" onClick={openCreate}>
            افزودن اولین لوکیشن
          </button>
        </div>
      ) : (
        <div className="locations-list" role="list">
          {locations.map((row) => {
            const cname =
              COUNTRIES.find((c) => c.code === row.country_code)?.name || row.country_code;
            return (
              <article key={row.id} className="locations-row" role="listitem">
                <div className="locations-flag" aria-hidden>
                  {row.flag}
                </div>
                <div className="locations-main">
                  <div
                    className="locations-host"
                    dir="ltr"
                    title={
                      row.match_host
                        ? `${row.host}:${row.port} · ${row.match_host}`
                        : `${row.host}:${row.port}`
                    }
                  >
                    <span className="locations-host-name">{row.host}</span>
                    <span className="locations-port">:{row.port}</span>
                  </div>
                  <div className="locations-country">
                    {cname}
                    {row.match_host ? (
                      <span className="locations-match" dir="ltr">
                        · {row.match_host}
                      </span>
                    ) : null}
                  </div>
                </div>
                <div className="locations-actions">
                  <button
                    type="button"
                    className="locations-icon-btn"
                    onClick={() => openEdit(row)}
                    title="ویرایش"
                    aria-label="ویرایش"
                  >
                    <Pencil size={16} strokeWidth={2} />
                  </button>
                  <button
                    type="button"
                    className="locations-icon-btn danger"
                    onClick={() => setConfirm(row)}
                    title="حذف"
                    aria-label="حذف"
                  >
                    <Trash2 size={16} strokeWidth={2} />
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {open && (
        <div className="modal-backdrop" onClick={() => !save.isPending && setOpen(false)}>
          <div
            className="modal locations-modal"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-labelledby="loc-modal-title"
          >
            <div className="modal-head">
              <div>
                <h2 id="loc-modal-title">{editing ? "ویرایش لوکیشن" : "لوکیشن جدید"}</h2>
                <p className="modal-sub">
                  دامنه و پورت سرور؛ هاست هدر اختیاری است (مثل host= در لینک)
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setOpen(false)}
                aria-label="بستن"
              >
                <X size={18} />
              </button>
            </div>

            <form className="locations-form" onSubmit={onSubmit}>
              <div className="locations-fields">
                <label className="locations-field">
                  <span>دامنه سرور</span>
                  <input
                    dir="ltr"
                    value={form.host}
                    onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))}
                    placeholder="c2.zynexoz.ir"
                    autoFocus
                    required
                    autoComplete="off"
                    spellCheck={false}
                  />
                </label>
                <label className="locations-field locations-field-port">
                  <span>پورت</span>
                  <input
                    dir="ltr"
                    inputMode="numeric"
                    value={form.port}
                    onChange={(e) => setForm((f) => ({ ...f, port: e.target.value }))}
                    placeholder="2095"
                    required
                    autoComplete="off"
                  />
                </label>
              </div>

              <label className="locations-field">
                <span>
                  هاست هدر / SNI <em className="locations-optional">(اختیاری)</em>
                </span>
                <input
                  dir="ltr"
                  value={form.match_host}
                  onChange={(e) => setForm((f) => ({ ...f, match_host: e.target.value }))}
                  placeholder="turkiye.varzeshfotsal.ir"
                  autoComplete="off"
                  spellCheck={false}
                />
                <small className="locations-field-hint">
                  اگر پر شود، فقط وقتی دامنه + پورت + همین مقدار با هم در کانفیگ باشند پرچم
                  ست می‌شود؛ وگرنه قانون فقط دامنه+پورت اعمال می‌شود.
                </small>
              </label>

              <label className="locations-field">
                <span>کشور</span>
                <select
                  value={form.country_code}
                  onChange={(e) => setForm((f) => ({ ...f, country_code: e.target.value }))}
                >
                  {COUNTRIES.map((c) => (
                    <option key={c.code} value={c.code}>
                      {flagEmoji(c.code)} {c.name}
                    </option>
                  ))}
                </select>
              </label>

              <div className="locations-preview" dir="ltr">
                <span className="locations-preview-flag">{previewFlag}</span>
                <code>#1. 50G | user {previewFlag}</code>
                <span className="locations-preview-note">{countryName}</span>
              </div>

              {save.error && (
                <p className="form-error">
                  {save.error instanceof Error ? save.error.message : "خطا"}
                </p>
              )}

              <div className="modal-foot">
                <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
                  انصراف
                </button>
                <button type="submit" className="btn" disabled={save.isPending}>
                  {save.isPending ? "در حال ذخیره…" : "ذخیره"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {confirm && (
        <div className="modal-backdrop" onClick={() => setConfirm(null)}>
          <div className="modal locations-modal" onClick={(e) => e.stopPropagation()} role="dialog">
            <div className="modal-head">
              <div>
                <h2>حذف لوکیشن</h2>
                <p className="modal-sub">
                  پرچم {confirm.flag} برای{" "}
                  <b dir="ltr">
                    {confirm.host}:{confirm.port}
                    {confirm.match_host ? ` · ${confirm.match_host}` : ""}
                  </b>{" "}
                  حذف شود؟
                </p>
              </div>
              <button type="button" className="modal-close" onClick={() => setConfirm(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="modal-foot">
              <button type="button" className="btn ghost" onClick={() => setConfirm(null)}>
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirm.id)}
              >
                حذف
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
