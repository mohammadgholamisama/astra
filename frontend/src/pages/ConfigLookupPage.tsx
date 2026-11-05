import { FormEvent, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Search, Server, Layers, Copy, Check } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { toFaDigits } from "../lib/format";

type Parsed = {
  link_preview: string;
  protocol: string | null;
  remark: string | null;
  config_tag: string | null;
  host: string;
  port: number;
  match_host: string;
};

type Hit = {
  panel_id: number;
  panel_name: string;
  panel_type: string | null;
  panel_is_active: boolean;
  group_id: number | null;
  group_name: string | null;
  group_config_tag: string | null;
  order_kind: string | null;
  source_remark: string | null;
  match_quality: string;
  host: string;
  port: number;
  match_host: string;
  accounts_count: number;
  sample_users: string[];
};

type LookupOut = {
  parsed: Parsed | null;
  hits: Hit[];
  links_found: number;
  detail: string | null;
};

const QUALITY_LABEL: Record<string, string> = {
  tag: "تطبیق تگ گروه",
  exact: "تطبیق دقیق",
  uuid: "تطبیق UUID",
  uri: "لینک یکسان",
  sni: "تطبیق SNI",
  host_port: "دامنه و پورت",
};

const ORDER_KIND_LABEL: Record<string, string> = {
  direct: "مستقیم",
  tunnel: "تانل",
  gaming: "گیمینگ",
  other: "سایر",
};

const TYPE_LABEL: Record<string, string> = {
  astra: "آسترا",
  sanaei: "سنایی",
  pasarguard: "پاسارگاد",
  marzban: "مرزبان",
  rebecca: "ربکا",
  guard: "گارد",
};

export default function ConfigLookupPage() {
  const [text, setText] = useState("");
  const [copied, setCopied] = useState(false);

  const lookup = useMutation({
    mutationFn: (payload: string) =>
      api<LookupOut>("/api/admin/config-lookup", {
        method: "POST",
        body: JSON.stringify({ text: payload }),
        timeoutMs: 20_000,
      }),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    lookup.mutate(t);
  }

  const result = lookup.data;
  const errText =
    lookup.isError
      ? lookup.error instanceof ApiError
        ? lookup.error.message
        : lookup.error instanceof Error
          ? lookup.error.message
          : "جستجو ناموفق بود"
      : null;

  async function copyPreview() {
    const p = result?.parsed?.link_preview;
    if (!p) return;
    try {
      await navigator.clipboard.writeText(p);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  }

  return (
    <div className="admin-page config-lookup-page">
      <h1 className="page-title">شناسایی کانفیگ</h1>
      <p className="page-sub">
        لینک یا نام روی ساب را بچسبانید تا <b>همان گروهی که کانفیگ از آن ساخته
        شده</b> مشخص شود (نه همه گروه‌هایی که همان پنل را دارند) — همراه نوع
        ترتیب (مستقیم / تانل / گیمینگ).
      </p>

      <form className="config-lookup-form" onSubmit={onSubmit}>
        <div className="field">
          <label>لینک یا نام کانفیگ</label>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={
              "vless://…\nیا مثلاً:\n#1. (تانل گیگی 5تومن(5)) 8.6G | user"
            }
            rows={5}
            dir="auto"
            spellCheck={false}
            autoFocus
          />
          <p className="field-hint">
            اگر چند لینک باشد، اولین لینک بررسی می‌شود. نام روی ساب از روی تگ
            ذخیره‌شدهٔ گروه پیدا می‌شود.
          </p>
        </div>
        {errText && <p className="error">{errText}</p>}
        <div className="config-lookup-actions">
          <button
            type="submit"
            className="btn"
            disabled={lookup.isPending || !text.trim()}
          >
            <Search size={16} />
            {lookup.isPending ? "در حال جستجو…" : "پیدا کن"}
          </button>
          {text.trim() && (
            <button
              type="button"
              className="btn ghost"
              disabled={lookup.isPending}
              onClick={() => {
                setText("");
                lookup.reset();
              }}
            >
              پاک کردن
            </button>
          )}
        </div>
      </form>

      {result && (
        <div className="config-lookup-result">
          {result.parsed && (
            <section className="config-lookup-parsed admin-card">
              <header className="config-lookup-card-head">
                <h3>کانفیگ خوانده‌شده</h3>
                <button
                  type="button"
                  className="btn ghost sm"
                  onClick={() => void copyPreview()}
                  title="کپی پیش‌نمایش"
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                  {copied ? "کپی شد" : "کپی"}
                </button>
              </header>
              <div className="config-lookup-meta">
                {result.parsed.remark ? (
                  <div>
                    <span className="muted">نام</span>
                    <strong dir="auto">{result.parsed.remark}</strong>
                  </div>
                ) : null}
                {result.parsed.config_tag ? (
                  <div>
                    <span className="muted">تگ گروه (از نام ساب)</span>
                    <strong dir="auto">{result.parsed.config_tag}</strong>
                  </div>
                ) : null}
                {result.parsed.protocol ? (
                  <div>
                    <span className="muted">پروتکل</span>
                    <strong dir="ltr">{result.parsed.protocol}</strong>
                  </div>
                ) : null}
                {result.parsed.host ? (
                  <div>
                    <span className="muted">دامنه / پورت</span>
                    <strong dir="ltr">
                      {result.parsed.host}
                      {result.parsed.port ? `:${result.parsed.port}` : ""}
                    </strong>
                  </div>
                ) : null}
                {result.parsed.match_host ? (
                  <div>
                    <span className="muted">SNI / هاست</span>
                    <strong dir="ltr">{result.parsed.match_host}</strong>
                  </div>
                ) : null}
              </div>
            </section>
          )}

          {result.detail && (
            <p
              className={
                result.hits.length ? "admin-toast ok" : "admin-toast err"
              }
            >
              {result.detail}
            </p>
          )}

          {result.hits.length === 0 ? (
            <div className="admin-empty">
              <Search size={36} strokeWidth={1.4} />
              <p>مالی در آسترا پیدا نشد</p>
            </div>
          ) : (
            <div className="config-lookup-hits">
              {result.hits.map((h) => (
                <article
                  key={`${h.panel_id}-${h.group_id ?? "x"}-${h.match_quality}`}
                  className={`admin-card config-lookup-hit${
                    h.panel_is_active ? "" : " is-off"
                  }`}
                >
                  <div className="config-lookup-hit-top">
                    <div className="config-lookup-hit-main">
                      <div className="config-lookup-hit-row">
                        <Layers size={16} />
                        <div>
                          <span className="muted">گروه سازنده</span>
                          <strong>
                            {h.group_id != null && h.group_name ? (
                              <Link
                                to={`/groups?group=${h.group_id}`}
                                title="باز کردن این گروه"
                              >
                                {h.group_name}
                              </Link>
                            ) : (
                              <span className="muted">— بدون گروه مشخص</span>
                            )}
                          </strong>
                        </div>
                      </div>
                      <div className="config-lookup-hit-row">
                        <Server size={16} />
                        <div>
                          <span className="muted">پنل</span>
                          <strong>
                            <Link
                              to={`/panels?panel=${h.panel_id}`}
                              title="باز کردن این پنل"
                            >
                              {h.panel_name}
                            </Link>
                          </strong>
                        </div>
                      </div>
                    </div>
                    <div className="admin-card-tags">
                      {h.panel_type ? (
                        <span className="badge muted">
                          {TYPE_LABEL[h.panel_type] || h.panel_type}
                        </span>
                      ) : null}
                      {h.order_kind ? (
                        <span className="badge muted">
                          {ORDER_KIND_LABEL[h.order_kind] || h.order_kind}
                        </span>
                      ) : null}
                      <span
                        className={`badge ${h.panel_is_active ? "ok" : "err"}`}
                      >
                        {h.panel_is_active ? "روشن" : "خاموش"}
                      </span>
                      <span className="badge ok">
                        {QUALITY_LABEL[h.match_quality] || h.match_quality}
                      </span>
                    </div>
                  </div>

                  {h.group_config_tag ? (
                    <p className="config-lookup-remark" dir="auto">
                      <span className="muted">متن روی کانفیگ این گروه: </span>
                      {h.group_config_tag}
                    </p>
                  ) : null}

                  {h.source_remark ? (
                    <p className="config-lookup-remark" dir="auto">
                      <span className="muted">نمونه / لیبل: </span>
                      {h.source_remark}
                    </p>
                  ) : null}

                  {(h.host || h.port) ? (
                    <div className="config-order-fp" dir="ltr">
                      <div className="config-order-fp-cell">
                        <span className="config-order-fp-k">دامنه</span>
                        <span className="config-order-fp-v">{h.host || "—"}</span>
                      </div>
                      <div className="config-order-fp-cell port">
                        <span className="config-order-fp-k">پورت</span>
                        <span className="config-order-fp-v">
                          {h.port ? toFaDigits(h.port) : "—"}
                        </span>
                      </div>
                      <div
                        className={`config-order-fp-cell host${
                          h.match_host ? "" : " empty"
                        }`}
                      >
                        <span className="config-order-fp-k">هاست / SNI</span>
                        <span className="config-order-fp-v">
                          {h.match_host || "—"}
                        </span>
                      </div>
                    </div>
                  ) : null}

                  {h.accounts_count > 0 ? (
                    <p className="config-lookup-accounts muted">
                      روی {toFaDigits(h.accounts_count)} اکانت ریموت دیده شد
                      {h.sample_users.length
                        ? ` — نمونه: ${h.sample_users.join("، ")}`
                        : ""}
                    </p>
                  ) : (
                    <p className="config-lookup-accounts muted">
                      از روی تگ / نمونه گروه پیدا شد
                    </p>
                  )}
                </article>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
