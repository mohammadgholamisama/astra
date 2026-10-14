import { FormEvent, useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Database,
  Download,
  FileArchive,
  Loader2,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import { ApiError, api } from "../lib/api";
import { toFaDigits } from "../lib/format";
import { useAuth } from "../store/auth";

type BackupStatus = {
  supported: boolean;
  dialect: string;
  path_name?: string;
  size_bytes?: number;
  exists?: boolean;
  format?: string;
  max_upload_bytes?: number;
  message?: string;
};

type RestoreResult = {
  ok: boolean;
  message: string;
  manifest?: {
    format?: string;
    created_at?: string;
    panel?: string;
    db_size?: number;
  };
  safety_copy?: string;
};

function formatBytes(n: number | undefined): string {
  const v = Number(n) || 0;
  if (v <= 0) return "۰";
  const units = ["بایت", "کیلوبایت", "مگابایت", "گیگابایت"];
  let x = v;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  const num = x >= 100 ? x.toFixed(0) : x >= 10 ? x.toFixed(1) : x.toFixed(2);
  return `${toFaDigits(num)} ${units[i]}`;
}

export default function BackupPage() {
  const qc = useQueryClient();
  const logout = useAuth((s) => s.logout);
  const nav = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [downloading, setDownloading] = useState(false);

  const { data: status, isLoading, error, refetch } = useQuery({
    queryKey: ["backup-status"],
    queryFn: () => api<BackupStatus>("/api/admin/backup/status"),
  });

  const pickFile = useCallback((next: File | null) => {
    if (!next) {
      setFile(null);
      return;
    }
    const name = next.name.toLowerCase();
    if (!name.endsWith(".zip")) {
      setMsg({ type: "err", text: "فقط فایل ZIP بک‌آپ Astra را انتخاب کنید" });
      return;
    }
    setFile(next);
    setMsg(null);
  }, []);

  const restore = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("فایل بک‌آپ را انتخاب کنید");
      const fd = new FormData();
      fd.append("file", file);
      return api<RestoreResult>("/api/admin/backup/restore", {
        method: "POST",
        body: fd,
      });
    },
    onSuccess: (res) => {
      setMsg({
        type: "ok",
        text:
          res.message +
          (res.safety_copy ? ` · کپی ایمنی: ${res.safety_copy}` : ""),
      });
      setFile(null);
      setConfirmText("");
      if (fileRef.current) fileRef.current.value = "";
      qc.clear();
      window.setTimeout(() => {
        logout();
        nav("/login", { replace: true });
      }, 2200);
    },
    onError: (e: Error) => {
      setMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : e.message,
      });
    },
  });

  async function downloadBackup() {
    setMsg(null);
    setDownloading(true);
    try {
      const token = localStorage.getItem("astra_token");
      const res = await fetch("/api/admin/backup", {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        let message = res.statusText || "دانلود ناموفق";
        try {
          const j = await res.json();
          const d = j?.detail;
          if (typeof d === "string") message = d;
          else if (d && typeof d === "object" && d.message) message = String(d.message);
        } catch {
          /* ignore */
        }
        throw new Error(message);
      }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const match = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
      const rawName = match ? decodeURIComponent(match[1].replace(/"/g, "")) : "";
      const filename = rawName || `Astra-backup-${Date.now()}.zip`;

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setMsg({ type: "ok", text: `دانلود شد: ${filename}` });
      void refetch();
    } catch (e) {
      setMsg({
        type: "err",
        text: e instanceof Error ? e.message : "دانلود بک‌آپ ناموفق بود",
      });
    } finally {
      setDownloading(false);
    }
  }

  function onRestoreSubmit(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (confirmText.trim() !== "بازیابی") {
      setMsg({ type: "err", text: "برای تأیید، دقیقاً کلمه «بازیابی» را بنویسید" });
      return;
    }
    restore.mutate();
  }

  const canRestore =
    !!file &&
    confirmText.trim() === "بازیابی" &&
    !restore.isPending &&
    status?.supported !== false;

  const confirmOk = confirmText.trim() === "بازیابی";

  return (
    <div className="admin-page backup-page">
      <header className="backup-hero">
        <div className="backup-hero-copy">
          <p className="backup-kicker">انتقال امن پنل</p>
          <h1 className="page-title">بک‌آپ و بازیابی</h1>
          <p className="page-sub">
            کل دیتابیس را دانلود کنید، روی سیستم دیگر آپلود کنید — کاربران، گروه‌ها، پنل‌ها و
            تنظیمات یکجا منتقل می‌شوند.
          </p>
        </div>
        <ol className="backup-steps" aria-label="مراحل">
          <li>
            <span className="backup-step-num">۱</span>
            <span>دانلود ZIP</span>
          </li>
          <li>
            <span className="backup-step-num">۲</span>
            <span>انتقال فایل</span>
          </li>
          <li>
            <span className="backup-step-num">۳</span>
            <span>بازیابی</span>
          </li>
        </ol>
      </header>

      {msg && (
        <div className={`backup-toast ${msg.type}`} role="status">
          {msg.type === "ok" ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
          <p>{msg.text}</p>
          <button
            type="button"
            className="backup-toast-close"
            onClick={() => setMsg(null)}
            aria-label="بستن"
          >
            <X size={16} />
          </button>
        </div>
      )}

      {isLoading && (
        <div className="backup-loading">
          <Loader2 size={22} className="spin" />
          <span>در حال خواندن وضعیت دیتابیس…</span>
        </div>
      )}
      {error && <p className="error">{(error as Error).message}</p>}

      {status && !status.supported && (
        <div className="backup-toast err">
          <AlertTriangle size={18} />
          <p>{status.message || "بک‌آپ روی این نوع دیتابیس فعال نیست"}</p>
        </div>
      )}

      {status?.supported && (
        <>
          <section className="backup-status-bar" aria-label="وضعیت دیتابیس">
            <div className="backup-status-item">
              <Database size={18} />
              <div>
                <span>فایل</span>
                <strong dir="ltr">{status.path_name || "—"}</strong>
              </div>
            </div>
            <div className="backup-status-item">
              <Archive size={18} />
              <div>
                <span>حجم فعلی</span>
                <strong>{formatBytes(status.size_bytes)}</strong>
              </div>
            </div>
            <div className="backup-status-item">
              <ShieldCheck size={18} />
              <div>
                <span>فرمت</span>
                <strong dir="ltr">{status.format || "—"}</strong>
              </div>
            </div>
          </section>

          <div className="backup-grid">
            <section className="backup-panel backup-panel-export">
              <div className="backup-panel-icon" aria-hidden>
                <Download size={22} />
              </div>
              <h2>دانلود بک‌آپ</h2>
              <p>
                اسنپ‌شات کامل و سازگار از دیتابیس زنده. فایل ZIP را در جای امن نگه دارید.
              </p>
              <ul className="backup-checklist">
                <li>همه کاربران و کانفیگ‌ها</li>
                <li>گروه‌ها، پنل‌ها و تنظیمات</li>
                <li>کلیدهای API و checksum اعتبارسنجی</li>
              </ul>
              <button
                type="button"
                className="btn backup-cta"
                onClick={() => void downloadBackup()}
                disabled={downloading || restore.isPending}
              >
                {downloading ? (
                  <>
                    <Loader2 size={18} className="spin" />
                    در حال آماده‌سازی…
                  </>
                ) : (
                  <>
                    <Download size={18} />
                    دانلود بک‌آپ کامل
                  </>
                )}
              </button>
            </section>

            <section className="backup-panel backup-panel-import">
              <div className="backup-panel-icon warn" aria-hidden>
                <Upload size={22} />
              </div>
              <h2>بازیابی بک‌آپ</h2>
              <p>دیتابیس فعلی با فایل ZIP عوض می‌شود. قبلش کپی ایمنی خودکار ساخته می‌شود.</p>

              <div className="backup-warn-inline">
                <AlertTriangle size={16} />
                <span>فقط ZIP خروجی همین پنل — بعد از بازیابی دوباره وارد شوید</span>
              </div>

              <form onSubmit={onRestoreSubmit} className="backup-restore-form">
                <div
                  className={`backup-dropzone${dragOver ? " over" : ""}${file ? " has-file" : ""}`}
                  onDragEnter={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragLeave={(e) => {
                    e.preventDefault();
                    setDragOver(false);
                  }}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragOver(false);
                    const dropped = e.dataTransfer.files?.[0] || null;
                    pickFile(dropped);
                    if (fileRef.current) fileRef.current.value = "";
                  }}
                  onClick={() => fileRef.current?.click()}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      fileRef.current?.click();
                    }
                  }}
                >
                  <input
                    ref={fileRef}
                    type="file"
                    accept=".zip,application/zip"
                    className="backup-file-input"
                    onChange={(e) => pickFile(e.target.files?.[0] || null)}
                  />
                  {file ? (
                    <div className="backup-file-chip">
                      <FileArchive size={28} />
                      <div className="backup-file-meta">
                        <strong dir="ltr" title={file.name}>
                          {file.name}
                        </strong>
                        <span>{formatBytes(file.size)}</span>
                      </div>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={(e) => {
                          e.stopPropagation();
                          pickFile(null);
                          if (fileRef.current) fileRef.current.value = "";
                        }}
                      >
                        تغییر
                      </button>
                    </div>
                  ) : (
                    <div className="backup-drop-hint">
                      <Upload size={28} strokeWidth={1.6} />
                      <strong>فایل را بکشید و رها کنید</strong>
                      <span>یا برای انتخاب کلیک کنید · فقط .zip</span>
                      {status.max_upload_bytes ? (
                        <span className="backup-drop-limit">
                          حداکثر {formatBytes(status.max_upload_bytes)}
                        </span>
                      ) : null}
                    </div>
                  )}
                </div>

                <div className="field backup-confirm-field">
                  <label>
                    برای تأیید بنویسید <code>بازیابی</code>
                  </label>
                  <input
                    value={confirmText}
                    onChange={(e) => setConfirmText(e.target.value)}
                    placeholder="بازیابی"
                    autoComplete="off"
                    disabled={restore.isPending}
                    className={confirmOk ? "is-ok" : undefined}
                  />
                </div>

                <button type="submit" className="btn danger backup-cta" disabled={!canRestore}>
                  {restore.isPending ? (
                    <>
                      <Loader2 size={18} className="spin" />
                      در حال بازیابی…
                    </>
                  ) : (
                    <>
                      <Upload size={18} />
                      بازیابی و جایگزینی دیتابیس
                    </>
                  )}
                </button>
              </form>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
