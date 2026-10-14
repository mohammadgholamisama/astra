import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Link2, RotateCcw, Save, Settings, ShieldCheck } from "lucide-react";
import { ApiError, api } from "../lib/api";

type SiteSettings = {
  sub_base_url: string;
  panel_base_url: string;
  brand_name: string;
  link_channel: string;
  link_support: string;
  link_app: string;
  ssl_available: boolean;
  panel_domain: string;
  sub_domain: string;
  defaults: {
    sub_base_url: string;
    panel_base_url: string;
    brand_name: string;
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
  const [sslPanelDomain, setSslPanelDomain] = useState("");
  const [sslSubDomain, setSslSubDomain] = useState("");
  const [curPass, setCurPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newPass2, setNewPass2] = useState("");
  const [passMsg, setPassMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["site-settings"],
    queryFn: () => api<SiteSettings>("/api/admin/settings"),
  });

  useEffect(() => {
    if (!data) return;
    setPanelBase(data.panel_base_url || "");
    setSubBase(data.sub_base_url || "");
    setBrand(data.brand_name || "");
    setLinkChannel(data.link_channel || "");
    setLinkSupport(data.link_support || "");
    setLinkApp(data.link_app || "");
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
        }),
      }),
    onSuccess: (res) => {
      qc.setQueryData(["site-settings"], res);
      setPanelBase(res.panel_base_url || "");
      setSubBase(res.sub_base_url || "");
      setLinkChannel(res.link_channel || "");
      setLinkSupport(res.link_support || "");
      setLinkApp(res.link_app || "");
      setMsg({ type: "ok", text: "تنظیمات ذخیره شد" });
    },
    onError: (e: Error) => {
      const text = e instanceof ApiError ? e.message : e.message;
      setMsg({ type: "err", text });
    },
  });

  const resetField = useMutation({
    mutationFn: (which: "panel" | "sub" | "brand") =>
      api<SiteSettings>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify(
          which === "panel"
            ? { reset_panel_base_url: true }
            : which === "sub"
              ? { reset_sub_base_url: true }
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
    mutationFn: (payload: { target: "panel" | "sub"; domain: string }) =>
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
      linkApp.trim() !== (data.link_app || "").trim());

  return (
    <div className="admin-page settings-page">
      <h1 className="page-title">تنظیمات</h1>
      <p className="page-sub">دامنه پنل، دامنه ساب، برند، SSL و لینک‌های صفحه اشتراک</p>

      {isLoading && <p className="muted">در حال بارگذاری…</p>}
      {error && <p className="error">{(error as Error).message}</p>}

      {data && (
        <form className="settings-stack" onSubmit={onSubmit}>
          <div className="settings-card">
            <header className="settings-card-head">
              <Settings size={20} />
              <div>
                <h2>دامنه‌ها و برند</h2>
                <p>آدرس عمومی پنل ادمین و لینک ساب کاربران</p>
              </div>
            </header>

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

            <div className="field">
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

          {data.ssl_available && (
            <div className="settings-card">
              <header className="settings-card-head">
                <ShieldCheck size={20} />
                <div>
                  <h2>گواهی SSL (Let’s Encrypt)</h2>
                  <p>دامنه باید به IP همین سرور اشاره کند · پورت ۸۰ آزاد باشد</p>
                </div>
              </header>

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
              <p className="field-hint">
                از ترمینال هم می‌توانید با دستور <code dir="ltr">x-astra</code> دامنه و SSL را تنظیم کنید.
              </p>
            </div>
          )}

          <div className="settings-card">
            <header className="settings-card-head">
              <KeyRound size={20} />
              <div>
                <h2>رمز ورود به پنل</h2>
                <p>تغییر رمز ادمین فعلی · حداقل ۸ کاراکتر</p>
              </div>
            </header>

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
            {passMsg && <p className={`admin-toast ${passMsg.type}`}>{passMsg.text}</p>}
            <button
              type="button"
              className="btn"
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
              <KeyRound size={16} />
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

          {msg && <p className={`admin-toast ${msg.type}`}>{msg.text}</p>}

          <footer className="settings-foot settings-foot-standalone">
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
    </div>
  );
}
