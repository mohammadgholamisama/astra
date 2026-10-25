import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  KeyRound,
  Link2,
  Megaphone,
  Plus,
  RotateCcw,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import { ApiError, ApiKey, api } from "../lib/api";
import { toFaDigits } from "../lib/format";

const DEFAULT_STATUS_LINE = "✨ {username} - {remain} - {expire} ✨";
const DEFAULT_STATUS_LABEL = "وضعیت";

const STATUS_SAMPLE_VARS: Record<string, string> = {
  username: "zprobe_7877de",
  user: "zprobe_7877de",
  remain: "1G",
  remain_traffic: "1G",
  used: "2.5G",
  used_traffic: "2.5G",
  limit: "3G",
  total: "3G",
  expire: "2d",
  time: "2d",
  days: "2d",
  used_pct: "83",
  remain_pct: "17",
};

const STATUS_VAR_HELP: { key: string; desc: string }[] = [
  { key: "{username}", desc: "نام کاربری" },
  { key: "{remain}", desc: "حجم باقی‌مانده (مثل 1G)" },
  { key: "{used}", desc: "حجم مصرف‌شده" },
  { key: "{limit}", desc: "سقف حجم (∞ اگر نامحدود)" },
  { key: "{expire}", desc: "زمان باقی‌مانده (مثل 2d)" },
  { key: "{used_pct}", desc: "درصد مصرف" },
  { key: "{remain_pct}", desc: "درصد باقی‌مانده" },
];

function previewStatusLine(template: string): string {
  const raw = (template || "").trim() || DEFAULT_STATUS_LINE;
  return raw.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, key: string) => {
    return STATUS_SAMPLE_VARS[key.toLowerCase()] ?? "";
  });
}

type SiteSettings = {
  sub_base_url: string;
  panel_base_url: string;
  brand_name: string;
  link_channel: string;
  link_support: string;
  link_app: string;
  promo_config_enabled: boolean;
  promo_config_text: string;
  promo_config_text_2: string;
  status_line_template: string;
  status_panel_label: string;
  ssl_available: boolean;
  panel_domain: string;
  sub_domain: string;
  defaults: {
    sub_base_url: string;
    panel_base_url: string;
    brand_name: string;
    status_line_template?: string;
    status_panel_label?: string;
  };
};

function hostFromUrl(url: string): string {
  const raw = (url || "").trim();
  if (!raw) return "";
  try {
    const u = new URL(raw.includes("://") ? raw : `https://${raw}`);
    return u.hostname || "";
  } catch {
    return raw.replace(/^https?:\/\//, "").split("/")[0] || "";
  }
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const [panelBase, setPanelBase] = useState("");
  const [subBase, setSubBase] = useState("");
  const [brand, setBrand] = useState("");
  const [linkChannel, setLinkChannel] = useState("");
  const [linkSupport, setLinkSupport] = useState("");
  const [linkApp, setLinkApp] = useState("");
  const [promoEnabled, setPromoEnabled] = useState(false);
  const [promoText, setPromoText] = useState("");
  const [promoText2, setPromoText2] = useState("");
  const [statusTemplate, setStatusTemplate] = useState(DEFAULT_STATUS_LINE);
  const [statusLabel, setStatusLabel] = useState(DEFAULT_STATUS_LABEL);
  const [sslPanelDomain, setSslPanelDomain] = useState("");
  const [sslSubDomain, setSslSubDomain] = useState("");
  const [sslNodeDomain, setSslNodeDomain] = useState("");
  const [curPass, setCurPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newPass2, setNewPass2] = useState("");
  const [passMsg, setPassMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [keyOpen, setKeyOpen] = useState(false);
  const [keyName, setKeyName] = useState("");
  const [keyNote, setKeyNote] = useState("");
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [confirmKey, setConfirmKey] = useState<ApiKey | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["site-settings"],
    queryFn: () => api<SiteSettings>("/api/admin/settings"),
  });

  const { data: apiKeys = [], isLoading: keysLoading } = useQuery({
    queryKey: ["api-keys"],
    queryFn: () => api<ApiKey[]>("/api/admin/api-keys"),
  });

  const createKey = useMutation({
    mutationFn: () =>
      api<ApiKey>("/api/admin/api-keys", {
        method: "POST",
        body: JSON.stringify({ name: keyName, note: keyNote || null }),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setCreatedKey(res.key || null);
      setKeyName("");
      setKeyNote("");
      setKeyOpen(false);
    },
  });

  const removeKey = useMutation({
    mutationFn: (id: number) => api(`/api/admin/api-keys/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      setConfirmKey(null);
    },
  });

  useEffect(() => {
    if (!data) return;
    setPanelBase(data.panel_base_url || "");
    setSubBase(data.sub_base_url || "");
    setBrand(data.brand_name || "");
    setLinkChannel(data.link_channel || "");
    setLinkSupport(data.link_support || "");
    setLinkApp(data.link_app || "");
    setPromoEnabled(!!data.promo_config_enabled);
    setPromoText(data.promo_config_text || "");
    setPromoText2(data.promo_config_text_2 || "");
    setStatusTemplate(data.status_line_template || DEFAULT_STATUS_LINE);
    setStatusLabel(data.status_panel_label || DEFAULT_STATUS_LABEL);
    setSslPanelDomain(data.panel_domain || hostFromUrl(data.panel_base_url) || "");
    setSslSubDomain(data.sub_domain || hostFromUrl(data.sub_base_url) || "");
  }, [data]);

  const save = useMutation({
    mutationFn: () =>
      api<SiteSettings>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          panel_base_url: panelBase.trim(),
          sub_base_url: subBase.trim(),
          brand_name: brand.trim(),
          link_channel: linkChannel.trim(),
          link_support: linkSupport.trim(),
          link_app: linkApp.trim(),
          promo_config_enabled: promoEnabled,
          promo_config_text: promoText.trim(),
          promo_config_text_2: promoText2.trim(),
          status_line_template: statusTemplate.trim() || DEFAULT_STATUS_LINE,
          status_panel_label: statusLabel.trim() || DEFAULT_STATUS_LABEL,
        }),
      }),
    onSuccess: (res) => {
      qc.setQueryData(["site-settings"], res);
      setPanelBase(res.panel_base_url || "");
      setSubBase(res.sub_base_url || "");
      setBrand(res.brand_name || "");
      setLinkChannel(res.link_channel || "");
      setLinkSupport(res.link_support || "");
      setLinkApp(res.link_app || "");
      setPromoEnabled(!!res.promo_config_enabled);
      setPromoText(res.promo_config_text || "");
      setPromoText2(res.promo_config_text_2 || "");
      setStatusTemplate(res.status_line_template || DEFAULT_STATUS_LINE);
      setStatusLabel(res.status_panel_label || DEFAULT_STATUS_LABEL);
      setMsg({ type: "ok", text: "تنظیمات ذخیره شد" });
    },
    onError: (e: Error) => {
      const text = e instanceof ApiError ? e.message : e.message;
      setMsg({ type: "err", text });
    },
  });

  const resetField = useMutation({
    mutationFn: (which: "panel" | "sub" | "brand" | "status") =>
      api<SiteSettings>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify(
          which === "panel"
            ? { reset_panel_base_url: true }
            : which === "sub"
              ? { reset_sub_base_url: true }
              : which === "status"
                ? { reset_status_line: true }
                : { reset_brand_name: true }
        ),
      }),
    onSuccess: (res) => {
      qc.setQueryData(["site-settings"], res);
      setPanelBase(res.panel_base_url || "");
      setSubBase(res.sub_base_url || "");
      setBrand(res.brand_name || "");
      setLinkChannel(res.link_channel || "");
      setLinkSupport(res.link_support || "");
      setLinkApp(res.link_app || "");
      setStatusTemplate(res.status_line_template || DEFAULT_STATUS_LINE);
      setStatusLabel(res.status_panel_label || DEFAULT_STATUS_LABEL);
      setMsg({ type: "ok", text: "به مقدار پیش‌فرض برگردانده شد" });
    },
    onError: (e: Error) => {
      setMsg({ type: "err", text: e instanceof ApiError ? e.message : e.message });
    },
  });

  const changePass = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; message: string }>("/api/admin/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: curPass,
          new_password: newPass,
        }),
      }),
    onSuccess: (res) => {
      setPassMsg({ type: "ok", text: res.message || "رمز تغییر کرد" });
      setCurPass("");
      setNewPass("");
      setNewPass2("");
    },
    onError: (e: Error) => {
      setPassMsg({ type: "err", text: e instanceof ApiError ? e.message : e.message });
    },
  });

  const issueSsl = useMutation({
    mutationFn: (payload: { target: "panel" | "sub" | "node"; domain: string }) =>
      api<{ ok: boolean; message: string }>("/api/admin/settings/ssl", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: async (res) => {
      setMsg({ type: "ok", text: res.message || "SSL صادر شد" });
      const fresh = await api<SiteSettings>("/api/admin/settings");
      qc.setQueryData(["site-settings"], fresh);
      setPanelBase(fresh.panel_base_url || "");
      setSubBase(fresh.sub_base_url || "");
      setSslPanelDomain(fresh.panel_domain || hostFromUrl(fresh.panel_base_url) || "");
      setSslSubDomain(fresh.sub_domain || hostFromUrl(fresh.sub_base_url) || "");
    },
    onError: (e: Error) => {
      setMsg({ type: "err", text: e instanceof ApiError ? e.message : e.message });
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setMsg(null);
    save.mutate();
  }

  const previewSub = `${(subBase || "").replace(/\/+$/, "")}/sub/…`;
  const dirty =
    !!data &&
    (panelBase.trim() !== (data.panel_base_url || "").trim() ||
      subBase.trim() !== (data.sub_base_url || "").trim() ||
      brand.trim() !== (data.brand_name || "").trim() ||
      linkChannel.trim() !== (data.link_channel || "").trim() ||
      linkSupport.trim() !== (data.link_support || "").trim() ||
      linkApp.trim() !== (data.link_app || "").trim() ||
      promoEnabled !== !!data.promo_config_enabled ||
      promoText.trim() !== (data.promo_config_text || "").trim() ||
      promoText2.trim() !== (data.promo_config_text_2 || "").trim());

  return (
    <div className="admin-page settings-page">
      <header className="settings-page-head">
        <div>
          <h1 className="page-title">تنظیمات</h1>
          <p className="page-sub">
            دامنه پنل و ساب، برند، تبلیغات، SSL، کلید API و لینک‌های صفحه اشتراک
          </p>
        </div>
      </header>

      {isLoading && <p className="muted">در حال بارگذاری…</p>}
      {error && <p className="error">{(error as Error).message}</p>}

      {data && (
        <form className="settings-stack" onSubmit={onSubmit}>
          <div className="settings-card settings-card-wide">
            <header className="settings-card-head">
              <Settings size={20} />
              <div>
                <h2>دامنه‌ها و برند</h2>
                <p>آدرس عمومی پنل ادمین و لینک ساب کاربران</p>
              </div>
            </header>

            <div className="settings-fields-grid">
              <div className="field">
                <label>دامنه / آدرس پایه پنل</label>
                <input
                  dir="ltr"
                  value={panelBase}
                  onChange={(e) => setPanelBase(e.target.value)}
                  placeholder="https://panel.example.com"
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="field-hint">
                  آدرس ورود ادمین · پیش‌فرض env:{" "}
                  <code dir="ltr">{data.defaults.panel_base_url || "—"}</code>
                </p>
                <button
                  type="button"
                  className="btn ghost sm settings-reset"
                  disabled={resetField.isPending}
                  onClick={() => resetField.mutate("panel")}
                >
                  <RotateCcw size={14} />
                  بازگشت به پیش‌فرض
                </button>
              </div>

              <div className="field">
                <label>دامنه / آدرس پایه ساب</label>
                <input
                  dir="ltr"
                  value={subBase}
                  onChange={(e) => setSubBase(e.target.value)}
                  placeholder="https://sub.example.com"
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="field-hint">
                  فقط http/https · بدون مسیر `/sub` · پیش‌فرض env:{" "}
                  <code dir="ltr">{data.defaults.sub_base_url || "—"}</code>
                </p>
                <p className="settings-preview" dir="ltr">
                  نمونه لینک: {previewSub}
                </p>
                <button
                  type="button"
                  className="btn ghost sm settings-reset"
                  disabled={resetField.isPending}
                  onClick={() => resetField.mutate("sub")}
                >
                  <RotateCcw size={14} />
                  بازگشت به پیش‌فرض
                </button>
              </div>
            </div>

            <div className="field settings-field-brand">
              <label>نام برند در صفحه ساب</label>
              <input
                value={brand}
                onChange={(e) => setBrand(e.target.value)}
                placeholder="Astra"
                maxLength={48}
              />
              <p className="field-hint">
                در هدر صفحه ساب · پیش‌فرض:{" "}
                <code>{data.defaults.brand_name || "Astra"}</code>
              </p>
              <button
                type="button"
                className="btn ghost sm settings-reset"
                disabled={resetField.isPending}
                onClick={() => resetField.mutate("brand")}
              >
                <RotateCcw size={14} />
                بازگشت به پیش‌فرض
              </button>
            </div>
          </div>

          <div className="settings-card settings-card-wide settings-status-line">
            <header className="settings-card-head">
              <Sparkles size={20} />
              <div>
                <h2>خط وضعیت ساب</h2>
                <p>
                  اولین کانفیگ نمایشی ساب (بدون پینگ). با متغیرها شخصی‌سازی کنید —
                  مقدارها در هر بار رفرش ساب به‌روز می‌شوند.
                </p>
              </div>
            </header>

            <div className="settings-status-body">
              <div className="field">
                <label>برچسب گروه (بالای خط)</label>
                <input
                  value={statusLabel}
                  onChange={(e) => setStatusLabel(e.target.value)}
                  placeholder={DEFAULT_STATUS_LABEL}
                  maxLength={48}
                />
              </div>

              <div className="field">
                <label>قالب خط وضعیت</label>
                <textarea
                  value={statusTemplate}
                  onChange={(e) => setStatusTemplate(e.target.value)}
                  placeholder={DEFAULT_STATUS_LINE}
                  maxLength={200}
                  rows={2}
                  dir="ltr"
                  className="settings-status-template"
                  spellCheck={false}
                />
                <span className="settings-promo-count">
                  {toFaDigits(statusTemplate.length)} / ۲۰۰
                </span>
              </div>

              <div className="settings-status-vars" aria-label="متغیرهای قابل استفاده">
                {STATUS_VAR_HELP.map((v) => (
                  <button
                    key={v.key}
                    type="button"
                    className="settings-status-var"
                    title={v.desc}
                    onClick={() =>
                      setStatusTemplate((cur) => `${cur}${cur.endsWith(" ") || !cur ? "" : " "}${v.key}`)
                    }
                  >
                    <code dir="ltr">{v.key}</code>
                    <span>{v.desc}</span>
                  </button>
                ))}
              </div>

              <div className="settings-status-preview" dir="ltr">
                <span className="settings-status-preview-label">
                  {statusLabel.trim() || DEFAULT_STATUS_LABEL}
                </span>
                <strong>{previewStatusLine(statusTemplate)}</strong>
                <p className="field-hint">
                  نمونه با مقادیر آزمایشی · قالب پیش‌فرض:{" "}
                  <code dir="ltr">{DEFAULT_STATUS_LINE}</code>
                </p>
              </div>

              <button
                type="button"
                className="btn ghost sm settings-reset"
                disabled={resetField.isPending}
                onClick={() => resetField.mutate("status")}
              >
                <RotateCcw size={14} />
                بازگشت به پیش‌فرض
              </button>
            </div>
          </div>

          <div
            className={`settings-card settings-card-wide settings-promo${
              promoEnabled ? " is-on" : ""
            }`}
          >
            <header className="settings-card-head settings-promo-head">
              <Megaphone size={20} />
              <div className="settings-promo-head-text">
                <h2>کانفیگ تبلیغاتی ساب</h2>
                <p>
                  بعد از خط وضعیت تا دو شعار تبلیغاتی در همه ساب‌ها نشان داده می‌شود —
                  بدون پینگ، فقط نمایش.
                </p>
              </div>
              <button
                type="button"
                className={`settings-switch${promoEnabled ? " on" : ""}`}
                role="switch"
                aria-checked={promoEnabled}
                aria-label={
                  promoEnabled
                    ? "خاموش کردن کانفیگ تبلیغاتی"
                    : "روشن کردن کانفیگ تبلیغاتی"
                }
                onClick={() => setPromoEnabled((v) => !v)}
              >
                <span className="settings-switch-track" aria-hidden>
                  <span className="settings-switch-knob" />
                </span>
                <span className="settings-switch-label">
                  {promoEnabled ? "روشن" : "خاموش"}
                </span>
              </button>
            </header>

            <div className={`settings-promo-body${promoEnabled ? "" : " is-disabled"}`}>
              <div className="settings-promo-slots">
                <div className="settings-promo-slot">
                  <div className="settings-promo-slot-head">
                    <span className="settings-promo-num">۱</span>
                    <div>
                      <strong>تبلیغ اول</strong>
                      <p>خط دوم ساب · اختیاری اگر هر دو خالی باشد چیزی اضافه نمی‌شود</p>
                    </div>
                  </div>
                  <textarea
                    value={promoText}
                    onChange={(e) => setPromoText(e.target.value)}
                    placeholder="کانفیگ پلاس | همیشه وصل باش😉"
                    maxLength={200}
                    rows={2}
                    disabled={!promoEnabled}
                  />
                  <span className="settings-promo-count">
                    {toFaDigits(promoText.length)} / ۲۰۰
                  </span>
                </div>

                <div className="settings-promo-slot">
                  <div className="settings-promo-slot-head">
                    <span className="settings-promo-num">۲</span>
                    <div>
                      <strong>تبلیغ دوم</strong>
                      <p>خط سوم ساب · اگر خالی باشد فقط تبلیغ اول می‌آید</p>
                    </div>
                  </div>
                  <textarea
                    value={promoText2}
                    onChange={(e) => setPromoText2(e.target.value)}
                    placeholder="کانال ما | @example"
                    maxLength={200}
                    rows={2}
                    disabled={!promoEnabled}
                  />
                  <span className="settings-promo-count">
                    {toFaDigits(promoText2.length)} / ۲۰۰
                  </span>
                </div>
              </div>
            </div>
          </div>

          {data.ssl_available && (
            <div className="settings-card settings-card-wide">
              <header className="settings-card-head">
                <ShieldCheck size={20} />
                <div>
                  <h2>گواهی SSL (Let’s Encrypt)</h2>
                  <p>دامنه باید به IP همین سرور اشاره کند · پورت ۸۰ آزاد باشد</p>
                </div>
              </header>

              <div className="settings-fields-grid">
                <div className="field">
                  <label>SSL دامنه پنل</label>
                  <input
                    dir="ltr"
                    value={sslPanelDomain}
                    onChange={(e) => setSslPanelDomain(e.target.value)}
                    placeholder="panel.example.com"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="btn ghost sm settings-reset"
                    disabled={issueSsl.isPending || !sslPanelDomain.trim()}
                    onClick={() => {
                      setMsg(null);
                      issueSsl.mutate({
                        target: "panel",
                        domain: sslPanelDomain.trim(),
                      });
                    }}
                  >
                    <ShieldCheck size={14} />
                    {issueSsl.isPending ? "در حال صدور…" : "دریافت SSL پنل"}
                  </button>
                </div>

                <div className="field">
                  <label>SSL دامنه ساب</label>
                  <input
                    dir="ltr"
                    value={sslSubDomain}
                    onChange={(e) => setSslSubDomain(e.target.value)}
                    placeholder="sub.example.com"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="btn ghost sm settings-reset"
                    disabled={issueSsl.isPending || !sslSubDomain.trim()}
                    onClick={() => {
                      setMsg(null);
                      issueSsl.mutate({
                        target: "sub",
                        domain: sslSubDomain.trim(),
                      });
                    }}
                  >
                    <ShieldCheck size={14} />
                    {issueSsl.isPending ? "در حال صدور…" : "دریافت SSL ساب"}
                  </button>
                </div>

                <div className="field">
                  <label>SSL دامنه اوتباند (نود)</label>
                  <input
                    dir="ltr"
                    value={sslNodeDomain}
                    onChange={(e) => setSslNodeDomain(e.target.value)}
                    placeholder="out.example.com"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="btn ghost sm settings-reset"
                    disabled={issueSsl.isPending || !sslNodeDomain.trim()}
                    onClick={() => {
                      setMsg(null);
                      issueSsl.mutate({
                        target: "node",
                        domain: sslNodeDomain.trim(),
                      });
                    }}
                  >
                    <ShieldCheck size={14} />
                    {issueSsl.isPending ? "در حال صدور…" : "دریافت SSL اوتباند"}
                  </button>
                </div>
              </div>
              <p className="field-hint">
                دامنهٔ اوتباند را بعد از SSL در صفحهٔ پنل‌ها به‌عنوان هاست عمومی بگذارید.
                از ترمینال هم می‌توانید با <code dir="ltr">x-astra</code> دامنه و SSL را تنظیم کنید.
              </p>
            </div>
          )}

          <div className="settings-card settings-card-wide">
            <header className="settings-card-head">
              <KeyRound size={20} />
              <div>
                <h2>کلیدهای API</h2>
                <p>
                  برای ربات تلگرام / اسکریپت — هدر{" "}
                  <code dir="ltr">X-Api-Key</code> · لیست کامل endpointها از منو: مستندات API
                </p>
              </div>
            </header>

            {createdKey && (
              <div className="admin-toast ok admin-key-reveal">
                <div>
                  <strong>کلید یک‌بار نمایش داده می‌شود — همین الان کپی کنید</strong>
                  <code dir="ltr" className="admin-key-value">
                    {createdKey}
                  </code>
                </div>
                <button
                  type="button"
                  className="btn ghost sm"
                  onClick={() => {
                    void navigator.clipboard.writeText(createdKey);
                  }}
                >
                  کپی
                </button>
              </div>
            )}

            <div className="toolbar" style={{ margin: "0 0 12px" }}>
              <span className="muted">{toFaDigits(apiKeys.length)} کلید</span>
              <button
                type="button"
                className="btn toolbar-primary"
                onClick={() => setKeyOpen(true)}
              >
                <Plus size={16} />
                ساخت کلید
              </button>
            </div>

            {keysLoading ? (
              <p className="muted">در حال بارگذاری…</p>
            ) : apiKeys.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                هنوز کلیدی ساخته نشده
              </p>
            ) : (
              <div className="groups-list">
                {apiKeys.map((k) => (
                  <article key={k.id} className="groups-row">
                    <div className="groups-row-main">
                      <div className="groups-row-title">
                        <b>{k.name}</b>
                        {k.note ? <span className="groups-row-desc">{k.note}</span> : null}
                      </div>
                      <div className="groups-row-meta">
                        <span className="badge muted" dir="ltr">
                          {k.key_prefix}…
                        </span>
                        <span className={`badge ${k.is_active ? "ok" : "err"}`}>
                          {k.is_active ? "فعال" : "غیرفعال"}
                        </span>
                      </div>
                    </div>
                    <div className="groups-row-actions">
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
          </div>

          <div className="settings-card">
            <header className="settings-card-head">
              <ShieldCheck size={20} />
              <div>
                <h2>رمز ورود به پنل</h2>
                <p>تغییر رمز ادمین فعلی · حداقل ۸ کاراکتر</p>
              </div>
            </header>

            <div className="settings-fields-grid settings-fields-grid-3">
              <div className="field">
                <label>رمز فعلی</label>
                <input
                  type="password"
                  value={curPass}
                  onChange={(e) => setCurPass(e.target.value)}
                  autoComplete="current-password"
                  placeholder="رمز فعلی"
                />
              </div>
              <div className="field">
                <label>رمز جدید</label>
                <input
                  type="password"
                  value={newPass}
                  onChange={(e) => setNewPass(e.target.value)}
                  autoComplete="new-password"
                  placeholder="حداقل ۸ کاراکتر"
                />
              </div>
              <div className="field">
                <label>تکرار رمز جدید</label>
                <input
                  type="password"
                  value={newPass2}
                  onChange={(e) => setNewPass2(e.target.value)}
                  autoComplete="new-password"
                  placeholder="تکرار رمز جدید"
                />
              </div>
            </div>
            {passMsg && <p className={`admin-toast ${passMsg.type}`}>{passMsg.text}</p>}
            <button
              type="button"
              className="btn settings-inline-action"
              disabled={
                changePass.isPending ||
                !curPass ||
                newPass.trim().length < 8 ||
                newPass !== newPass2
              }
              onClick={() => {
                setPassMsg(null);
                if (newPass !== newPass2) {
                  setPassMsg({ type: "err", text: "تکرار رمز با رمز جدید یکی نیست" });
                  return;
                }
                changePass.mutate();
              }}
            >
              <ShieldCheck size={16} />
              {changePass.isPending ? "در حال تغییر…" : "تغییر رمز عبور"}
            </button>
          </div>

          <div className="settings-card">
            <header className="settings-card-head">
              <Link2 size={20} />
              <div>
                <h2>لینک‌های صفحه ساب</h2>
                <p>خالی بگذارید تا در صفحه اشتراک نمایش داده نشود</p>
              </div>
            </header>

            <div className="settings-fields-grid">
              <div className="field">
                <label>لینک کانال</label>
                <input
                  dir="ltr"
                  value={linkChannel}
                  onChange={(e) => setLinkChannel(e.target.value)}
                  placeholder="https://t.me/..."
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="field-hint">دکمه «کانال» در صفحه ساب · خالی = مخفی</p>
              </div>

              <div className="field">
                <label>لینک پشتیبان</label>
                <input
                  dir="ltr"
                  value={linkSupport}
                  onChange={(e) => setLinkSupport(e.target.value)}
                  placeholder="https://t.me/..."
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="field-hint">دکمه «پشتیبان» در صفحه ساب · خالی = مخفی</p>
              </div>
            </div>

            <div className="field">
              <label>لینک دانلود برنامه</label>
              <input
                dir="ltr"
                value={linkApp}
                onChange={(e) => setLinkApp(e.target.value)}
                placeholder="https://..."
                autoComplete="off"
                spellCheck={false}
              />
              <p className="field-hint">دکمه «دانلود برنامه» در صفحه ساب · خالی = مخفی</p>
            </div>
          </div>

          {msg && (
            <p className={`admin-toast settings-toast-span ${msg.type}`}>{msg.text}</p>
          )}

          <footer className="settings-foot settings-foot-bar">
            <div className="settings-foot-hint">
              {dirty ? "تغییرات ذخیره‌نشده دارید" : "همه‌چیز ذخیره‌شده است"}
            </div>
            <button
              type="submit"
              className="btn"
              disabled={
                save.isPending ||
                !dirty ||
                !panelBase.trim() ||
                !subBase.trim() ||
                !brand.trim()
              }
            >
              <Save size={16} />
              {save.isPending ? "در حال ذخیره…" : "ذخیره تنظیمات"}
            </button>
          </footer>
        </form>
      )}

      {keyOpen && (
        <div
          className="modal-backdrop"
          onClick={() => !createKey.isPending && setKeyOpen(false)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>ساخت کلید API</h3>
                <p className="modal-sub">کلید فقط یک‌بار بعد از ساخت نشان داده می‌شود</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setKeyOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                createKey.mutate();
              }}
            >
              <div className="field">
                <label>نام</label>
                <input
                  required
                  minLength={2}
                  value={keyName}
                  onChange={(e) => setKeyName(e.target.value)}
                  placeholder="مثلاً ربات تلگرام"
                />
              </div>
              <div className="field">
                <label>یادداشت (اختیاری)</label>
                <input value={keyNote} onChange={(e) => setKeyNote(e.target.value)} />
              </div>
              {createKey.isError && (
                <p className="error">{(createKey.error as Error).message}</p>
              )}
              <footer className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={createKey.isPending}
                  onClick={() => setKeyOpen(false)}
                >
                  انصراف
                </button>
                <button className="btn" disabled={createKey.isPending || keyName.trim().length < 2}>
                  {createKey.isPending ? "…" : "ساخت"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {confirmKey && (
        <div
          className="modal-backdrop"
          onClick={() => !removeKey.isPending && setConfirmKey(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>حذف کلید</h3>
                <p className="modal-sub">«{confirmKey.name}» دیگر کار نمی‌کند</p>
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
            {removeKey.isError && (
              <p className="error">{(removeKey.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={removeKey.isPending}
                onClick={() => setConfirmKey(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={removeKey.isPending}
                onClick={() => removeKey.mutate(confirmKey.id)}
              >
                {removeKey.isPending ? "…" : "حذف"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
