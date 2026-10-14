import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { api, ApiKey } from "../lib/api";
import { toFaDigits } from "../lib/format";

export default function ApiKeysPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [confirmKey, setConfirmKey] = useState<ApiKey | null>(null);

  const { data = [], isLoading } = useQuery({
    queryKey: ["api-keys"],
    queryFn: () => api<ApiKey[]>("/api/admin/api-keys"),
  });

  const create = useMutation({
    mutationFn: () =>
      api<ApiKey>("/api/admin/api-keys", {
        method: "POST",
        body: JSON.stringify({ name, note: note || null }),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setCreatedKey(res.key || null);
      setName("");
      setNote("");
      setOpen(false);
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/admin/api-keys/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setConfirmKey(null);
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }

  return (
    <div className="admin-page">
      <h1 className="page-title">کلید API</h1>
      <p className="page-sub">
        برای اتصال ربات‌ها — هدر <code>X-Api-Key</code>
      </p>

      <div className="toolbar">
        <span className="muted">{toFaDigits(data.length)} کلید</span>
        <button className="btn toolbar-primary" onClick={() => setOpen(true)}>
          <Plus size={16} />
          ساخت کلید
        </button>
      </div>

      {createdKey && (
        <div className="admin-toast ok admin-key-reveal">
          <div>
            <strong>کلید فقط یک‌بار نمایش داده می‌شود</strong>
            <input readOnly value={createdKey} onFocus={(e) => e.target.select()} dir="ltr" />
          </div>
          <div className="row-actions">
            <button className="btn sm" onClick={() => navigator.clipboard.writeText(createdKey)}>
              کپی
            </button>
            <button className="btn ghost sm" onClick={() => setCreatedKey(null)}>
              بستن
            </button>
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
        </div>
      ) : data.length === 0 ? (
        <div className="admin-empty">
          <KeyRound size={36} strokeWidth={1.4} />
          <p>کلیدی ساخته نشده</p>
          <button className="btn" onClick={() => setOpen(true)}>
            ساخت اولین کلید
          </button>
        </div>
      ) : (
        <div className="admin-grid">
          {data.map((k) => (
            <article key={k.id} className={`admin-card ${k.is_active ? "" : "is-off"}`}>
              <div className="admin-card-top">
                <div className="admin-card-id">
                  <b>{k.name}</b>
                  <div className="admin-card-tags">
                    <span className={`badge ${k.is_active ? "ok" : "err"}`}>
                      {k.is_active ? "فعال" : "غیرفعال"}
                    </span>
                    <span className="badge muted" dir="ltr">
                      {k.key_prefix}…
                    </span>
                  </div>
                </div>
              </div>
              {k.note && <p className="admin-card-desc">{k.note}</p>}
              <div className="admin-card-actions">
                <button
                  type="button"
                  className="btn danger sm"
                  onClick={() => setConfirmKey(k)}
                >
                  <Trash2 size={14} />
                  حذف
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {confirmKey && (
        <div
          className="modal-backdrop"
          onClick={() => !remove.isPending && setConfirmKey(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>تأیید حذف کلید</h3>
                <p className="modal-sub">ربات‌هایی که از این کلید استفاده می‌کنند دیگر دسترسی ندارند</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmKey(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              مطمئنید می‌خواهید کلید <strong>{confirmKey.name}</strong>{" "}
              <span className="muted" dir="ltr">
                ({confirmKey.key_prefix}…)
              </span>{" "}
              را حذف کنید؟
              <br />
              <span className="muted" style={{ fontSize: "0.9rem" }}>
                این کار قابل برگشت نیست.
              </span>
            </p>
            {remove.isError && <p className="error">{(remove.error as Error).message}</p>}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={remove.isPending}
                onClick={() => setConfirmKey(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirmKey.id)}
              >
                {remove.isPending ? "در حال حذف…" : "بله، حذف شود"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>کلید جدید</h3>
                <p className="modal-sub">برای ربات یا کلاینت خارجی</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form onSubmit={onSubmit}>
              <div className="field">
                <label>نام</label>
                <input required value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="field">
                <label>یادداشت</label>
                <input
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="ربات فروش"
                />
              </div>
              {create.isError && <p className="error">{(create.error as Error).message}</p>}
              <footer className="modal-foot">
                <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
                  انصراف
                </button>
                <button className="btn" disabled={create.isPending}>
                  {create.isPending ? "…" : "ساخت"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
