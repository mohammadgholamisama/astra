import { FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Server, Trash2, Wifi } from "lucide-react";
import Select from "../components/Select";
import { api, Panel, PanelType } from "../lib/api";
import { toFaDigits } from "../lib/format";

type InboundInfo = {
  id: number;
  remark: string;
  protocol: string;
  port: number | null;
  enable: boolean;
};

type RemoteGroupInfo = {
  id: number;
  name: string;
  is_disabled: boolean;
};

type MarzProtocolInfo = {
  protocol: string;
  tags: string[];
  inbound_count: number;
  inbounds?: {
    tag: string;
    protocol: string;
    network: string;
    tls: string;
    port: number | string | null;
  }[];
};

const TYPES: { value: PanelType; label: string; hint: string }[] = [
  {
    value: "sanaei",
    label: "سنایی (3x-ui)",
    hint: "یوزر + پسورد + API Token — انتخاب اینباند با ID",
  },
  {
    value: "pasarguard",
    label: "پاسارگاد",
    hint: "یوزر/پسورد اوپراتور یا ادمین — آدرس بدون /dashboard",
  },
  {
    value: "marzban",
    label: "مرزبان",
    hint: "یوزر و پسورد ادمین — انتخاب پروتکل (vless/vmess/...)",
  },
  {
    value: "rebecca",
    label: "ربکا",
    hint: "یوزر و پسورد ادمین — کانفیگ از سرویس‌های پنل (بدون انتخاب پروتکل)",
  },
];

function parseMarzProtocols(meta: Record<string, unknown>): string[] {
  const raw = meta.protocols;
  if (Array.isArray(raw)) {
    return raw.map(String).filter(Boolean);
  }
  const inbounds = meta.inbounds;
  if (inbounds && typeof inbounds === "object" && !Array.isArray(inbounds)) {
    return Object.keys(inbounds as Record<string, unknown>);
  }
  return [];
}

function protocolsToInboundsMeta(
  selected: string[],
  catalog: MarzProtocolInfo[]
): Record<string, string[]> {
  const map = new Map(catalog.map((p) => [p.protocol, p.tags]));
  const out: Record<string, string[]> = {};
  for (const protocol of selected) {
    out[protocol] = [...(map.get(protocol) || [])];
  }
  return out;
}

/** Strip UI paths like /dashboard that break API calls */
function normalizeBaseUrl(url: string) {
  let u = url.trim().replace(/\/+$/, "");
  u = u.replace(/\/(dashboard|admin|panel|ui|app)$/i, "");
  u = u.replace(/\/api(\/admin)?$/i, "");
  return u.replace(/\/+$/, "");
}

const emptyForm = {
  name: "",
  type: "marzban" as PanelType,
  base_url: "",
  username: "",
  password: "",
  api_token: "",
  is_active: true,
};

function inboundLabel(ib: InboundInfo) {
  const title = ib.remark?.trim() || `Inbound #${ib.id}`;
  const meta = [ib.protocol, ib.port != null ? `:${ib.port}` : ""].filter(Boolean).join(" ");
  return meta ? `${title} · ${meta}` : title;
}

export default function PanelsPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Panel | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [selectedInbounds, setSelectedInbounds] = useState<number[]>([]);
  const [inbounds, setInbounds] = useState<InboundInfo[]>([]);
  const [inboundsLoading, setInboundsLoading] = useState(false);
  const [inboundsErr, setInboundsErr] = useState("");
  const [selectedGroups, setSelectedGroups] = useState<number[]>([]);
  const [remoteGroups, setRemoteGroups] = useState<RemoteGroupInfo[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsErr, setGroupsErr] = useState("");
  const [selectedProtocols, setSelectedProtocols] = useState<string[]>([]);
  const [marzProtocols, setMarzProtocols] = useState<MarzProtocolInfo[]>([]);
  const [marzLoading, setMarzLoading] = useState(false);
  const [marzErr, setMarzErr] = useState("");
  const [msg, setMsg] = useState("");
  const [confirmPanel, setConfirmPanel] = useState<Panel | null>(null);

  const typeMeta = useMemo(() => TYPES.find((t) => t.value === form.type), [form.type]);
  const isSanaei = form.type === "sanaei";
  const isPasarguard = form.type === "pasarguard";
  const isMarzban = form.type === "marzban";
  const isRebecca = form.type === "rebecca";
  const isEdit = !!editing;

  const { data = [], isLoading } = useQuery({
    queryKey: ["panels"],
    queryFn: () => api<Panel[]>("/api/admin/panels"),
  });

  function resetModal() {
    setOpen(false);
    setEditing(null);
    setForm(emptyForm);
    setSelectedInbounds([]);
    setInbounds([]);
    setInboundsErr("");
    setInboundsLoading(false);
    setSelectedGroups([]);
    setRemoteGroups([]);
    setGroupsErr("");
    setGroupsLoading(false);
    setSelectedProtocols([]);
    setMarzProtocols([]);
    setMarzErr("");
    setMarzLoading(false);
  }

  function openCreate() {
    setEditing(null);
    setForm(emptyForm);
    setSelectedInbounds([]);
    setInbounds([]);
    setInboundsErr("");
    setSelectedGroups([]);
    setRemoteGroups([]);
    setGroupsErr("");
    setSelectedProtocols([]);
    setMarzProtocols([]);
    setMarzErr("");
    setMsg("");
    setOpen(true);
  }

  function openEdit(p: Panel) {
    const meta = p.meta || {};
    const ids = Array.isArray(meta.inbound_ids)
      ? (meta.inbound_ids as unknown[]).map(Number).filter((n) => !Number.isNaN(n))
      : [];
    const gids = Array.isArray(meta.default_group_ids)
      ? (meta.default_group_ids as unknown[]).map(Number).filter((n) => !Number.isNaN(n))
      : [];
    setEditing(p);
    setForm({
      name: p.name,
      type: p.type,
      base_url: p.base_url,
      username: p.username || "",
      password: "",
      api_token: "",
      is_active: p.is_active,
    });
    setSelectedInbounds(ids);
    setInbounds([]);
    setInboundsErr("");
    setSelectedGroups(gids);
    setRemoteGroups([]);
    setGroupsErr("");
    setSelectedProtocols(p.type === "marzban" ? parseMarzProtocols(meta) : []);
    setMarzProtocols([]);
    setMarzErr("");
    setMsg("");
    setOpen(true);
  }

  async function loadInbounds() {
    if (!isSanaei) return;
    setInboundsLoading(true);
    setInboundsErr("");
    try {
      let list: InboundInfo[];
      if (isEdit && editing) {
        list = await api<InboundInfo[]>(`/api/admin/panels/${editing.id}/inbounds`);
      } else {
        if (!form.base_url.trim() || !form.api_token.trim()) {
          setInboundsErr("آدرس و API Token را وارد کنید");
          setInboundsLoading(false);
          return;
        }
        list = await api<InboundInfo[]>("/api/admin/panels/preview-inbounds", {
          method: "POST",
          body: JSON.stringify({
            base_url: normalizeBaseUrl(form.base_url),
            username: form.username || null,
            password: form.password || null,
            api_token: form.api_token,
          }),
        });
      }
      setInbounds(list);
      if (!list.length) setInboundsErr("اینباندی در پنل پیدا نشد");
    } catch (e) {
      setInbounds([]);
      setInboundsErr(e instanceof Error ? e.message : "خطا در دریافت اینباندها");
    } finally {
      setInboundsLoading(false);
    }
  }

  async function loadRemoteGroups() {
    if (!isPasarguard) return;
    setGroupsLoading(true);
    setGroupsErr("");
    try {
      let list: RemoteGroupInfo[];
      if (isEdit && editing) {
        list = await api<RemoteGroupInfo[]>(`/api/admin/panels/${editing.id}/groups`);
      } else {
        if (!form.base_url.trim() || !form.username.trim() || !form.password.trim()) {
          setGroupsErr("آدرس، نام کاربری و رمز عبور را وارد کنید");
          setGroupsLoading(false);
          return;
        }
        list = await api<RemoteGroupInfo[]>("/api/admin/panels/preview-groups", {
          method: "POST",
          body: JSON.stringify({
            base_url: normalizeBaseUrl(form.base_url),
            username: form.username,
            password: form.password,
            api_token: form.api_token || null,
          }),
        });
      }
      setRemoteGroups(list);
      if (!list.length) setGroupsErr("گروهی در پنل پیدا نشد");
    } catch (e) {
      setRemoteGroups([]);
      setGroupsErr(e instanceof Error ? e.message : "خطا در دریافت گروه‌ها");
    } finally {
      setGroupsLoading(false);
    }
  }

  useEffect(() => {
    if (!open || !isSanaei || !isEdit || !editing) return;
    void loadInbounds();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isSanaei, isEdit, editing?.id]);

  async function loadMarzProtocols() {
    if (!isMarzban) return;
    setMarzLoading(true);
    setMarzErr("");
    try {
      let list: MarzProtocolInfo[];
      if (isEdit && editing) {
        list = await api<MarzProtocolInfo[]>(`/api/admin/panels/${editing.id}/marz-protocols`);
      } else {
        if (!form.base_url.trim() || !form.username.trim() || !form.password.trim()) {
          setMarzErr("آدرس، نام کاربری و رمز عبور را وارد کنید");
          setMarzLoading(false);
          return;
        }
        list = await api<MarzProtocolInfo[]>("/api/admin/panels/preview-marz-protocols", {
          method: "POST",
          body: JSON.stringify({
            base_url: normalizeBaseUrl(form.base_url),
            username: form.username,
            password: form.password,
            panel_type: form.type,
          }),
        });
      }
      setMarzProtocols(list);
      if (!list.length) setMarzErr("پروتکلی در پنل پیدا نشد");
    } catch (e) {
      setMarzProtocols([]);
      setMarzErr(e instanceof Error ? e.message : "خطا در دریافت پروتکل‌ها");
    } finally {
      setMarzLoading(false);
    }
  }

  useEffect(() => {
    if (!open || !isPasarguard || !isEdit || !editing) return;
    void loadRemoteGroups();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isPasarguard, isEdit, editing?.id]);

  useEffect(() => {
    if (!open || !isMarzban || !isEdit || !editing) return;
    void loadMarzProtocols();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isMarzban, isEdit, editing?.id]);

  function toggleInbound(id: number) {
    setSelectedInbounds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function removeSelected(id: number) {
    setSelectedInbounds((prev) => prev.filter((x) => x !== id));
  }

  function toggleGroup(id: number) {
    setSelectedGroups((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }

  function removeSelectedGroup(id: number) {
    setSelectedGroups((prev) => prev.filter((x) => x !== id));
  }

  function toggleProtocol(protocol: string) {
    setSelectedProtocols((prev) =>
      prev.includes(protocol) ? prev.filter((x) => x !== protocol) : [...prev, protocol]
    );
  }

  function removeSelectedProtocol(protocol: string) {
    setSelectedProtocols((prev) => prev.filter((x) => x !== protocol));
  }

  function buildMeta(): Record<string, unknown> {
    const meta: Record<string, unknown> = isEdit ? { ...(editing?.meta || {}) } : {};
    if (isSanaei) {
      if (selectedInbounds.length) meta.inbound_ids = selectedInbounds;
      else delete meta.inbound_ids;
    } else {
      delete meta.inbound_ids;
    }
    if (isPasarguard) {
      if (selectedGroups.length) meta.default_group_ids = selectedGroups;
      else delete meta.default_group_ids;
    } else {
      delete meta.default_group_ids;
    }
    if (isMarzban) {
      if (selectedProtocols.length) {
        meta.protocols = selectedProtocols;
        meta.inbounds = protocolsToInboundsMeta(selectedProtocols, marzProtocols);
      } else {
        delete meta.protocols;
        delete meta.inbounds;
      }
      delete meta.service_id;
      delete meta.service_ids;
    } else if (isRebecca) {
      // Rebecca: configs come from Services — no protocol/inbound meta
      delete meta.protocols;
      delete meta.inbounds;
      delete meta.service_ids;
      // optional default service kept only if previously set via group; panel form does not set it
    } else {
      delete meta.protocols;
      delete meta.inbounds;
      delete meta.service_id;
      delete meta.service_ids;
    }
    return meta;
  }

  const save = useMutation({
    mutationFn: async () => {
      const meta = buildMeta();
      if (isEdit && editing) {
        const body: Record<string, unknown> = {
          name: form.name,
          base_url: normalizeBaseUrl(form.base_url),
          username: form.username || null,
          is_active: form.is_active,
          meta,
        };
        if (form.password.trim()) body.password = form.password;
        if (form.api_token.trim()) body.api_token = form.api_token;
        return api<Panel>(`/api/admin/panels/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      return api<Panel>("/api/admin/panels", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          type: form.type,
          base_url: normalizeBaseUrl(form.base_url),
          username: form.username || null,
          password: form.password || null,
          api_token: isSanaei ? form.api_token || null : form.api_token || null,
          is_active: form.is_active,
          meta,
        }),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["panels"] });
      resetModal();
      setMsg(isEdit ? "پنل به‌روزرسانی شد" : "اتصال موفق بود — پنل اضافه شد");
    },
    onError: (e: Error) => setMsg(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/admin/panels/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["panels"] });
      setConfirmPanel(null);
      setMsg("پنل حذف شد");
    },
    onError: (e: Error) => {
      setConfirmPanel(null);
      setMsg(e.message);
    },
  });

  const test = useMutation({
    mutationFn: (id: number) =>
      api<{ success: boolean; error?: string }>(`/api/admin/panels/${id}/test`, {
        method: "POST",
      }),
    onSuccess: (res) => {
      setMsg(res.success ? "اتصال موفق بود" : `خطا: ${res.error || "ناموفق"}`);
      qc.invalidateQueries({ queryKey: ["panels"] });
    },
    onError: (e: Error) => setMsg(e.message),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!isEdit && isSanaei && !form.api_token.trim()) {
      setMsg("برای سنایی API Token الزامی است");
      return;
    }
    if (!isEdit && (!form.username.trim() || !form.password.trim())) {
      setMsg("نام کاربری و رمز عبور الزامی است");
      return;
    }
    if (isEdit && !form.username.trim()) {
      setMsg("نام کاربری الزامی است");
      return;
    }
    save.mutate();
  }

  const selectedSet = useMemo(() => new Set(selectedInbounds), [selectedInbounds]);
  const selectedDetails = useMemo(() => {
    const map = new Map(inbounds.map((i) => [i.id, i]));
    return selectedInbounds.map((id) => map.get(id) || { id, remark: "", protocol: "", port: null, enable: true });
  }, [selectedInbounds, inbounds]);

  const selectedGroupSet = useMemo(() => new Set(selectedGroups), [selectedGroups]);
  const selectedGroupDetails = useMemo(() => {
    const map = new Map(remoteGroups.map((g) => [g.id, g]));
    return selectedGroups.map(
      (id) => map.get(id) || { id, name: `Group #${id}`, is_disabled: false }
    );
  }, [selectedGroups, remoteGroups]);

  const selectedProtocolSet = useMemo(() => new Set(selectedProtocols), [selectedProtocols]);
  const selectedProtocolDetails = useMemo(() => {
    const map = new Map(marzProtocols.map((p) => [p.protocol, p]));
    return selectedProtocols.map(
      (protocol) =>
        map.get(protocol) || {
          protocol,
          tags: [],
          inbound_count: 0,
        }
    );
  }, [selectedProtocols, marzProtocols]);

  return (
    <div className="admin-page">
      <h1 className="page-title">پنل‌ها</h1>
      <p className="page-sub">اتصال به سنایی، پاسارگاد، مرزبان و ربکا</p>

      <div className="toolbar">
        <span className="muted">{toFaDigits(data.length)} پنل</span>
        <button className="btn toolbar-primary" onClick={openCreate}>
          <Plus size={16} />
          افزودن پنل
        </button>
      </div>

      {msg && (
        <p
          className={
            msg.includes("موفق") || msg.includes("به‌روز") ? "admin-toast ok" : "admin-toast err"
          }
        >
          {msg}
        </p>
      )}

      {isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : data.length === 0 ? (
        <div className="admin-empty">
          <Server size={36} strokeWidth={1.4} />
          <p>هنوز پنلی اضافه نشده</p>
          <button className="btn" onClick={openCreate}>
            افزودن اولین پنل
          </button>
        </div>
      ) : (
        <div className="admin-grid">
          {data.map((p) => {
            const typeLabel = TYPES.find((t) => t.value === p.type)?.label || p.type;
            const credOk = p.type === "sanaei" ? p.has_token : p.has_password;
            const credLabel =
              p.type === "sanaei"
                ? p.has_token
                  ? "توکن دارد"
                  : "بدون توکن"
                : p.has_password
                  ? "رمز تنظیم شده"
                  : "رمز ناقص";
            return (
              <article key={p.id} className={`admin-card ${p.is_active ? "" : "is-off"}`}>
                <div className="admin-card-top">
                  <div className="admin-card-id">
                    <b>{p.name}</b>
                    <div className="admin-card-tags">
                      <span className="badge muted">{typeLabel}</span>
                      <span className={`badge ${p.is_active ? "ok" : "err"}`}>
                        {p.is_active ? "فعال" : "غیرفعال"}
                      </span>
                      <span className={`badge ${credOk ? "ok" : "err"}`}>{credLabel}</span>
                    </div>
                  </div>
                </div>
                <p className="admin-card-url" dir="ltr" title={p.base_url}>
                  {p.base_url}
                </p>
                <div className="admin-card-actions">
                  <button type="button" className="btn ghost sm" onClick={() => openEdit(p)}>
                    <Pencil size={14} />
                    ویرایش
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() => test.mutate(p.id)}
                    disabled={test.isPending}
                  >
                    <Wifi size={14} />
                    تست
                  </button>
                  <button
                    type="button"
                    className="btn danger sm"
                    onClick={() => setConfirmPanel(p)}
                  >
                    <Trash2 size={14} />
                    حذف
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {confirmPanel && (
        <div
          className="modal-backdrop"
          onClick={() => !remove.isPending && setConfirmPanel(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>تأیید حذف پنل</h3>
                <p className="modal-sub">
                  فقط از سایت Astra حذف می‌شود — پنل ریموت و کاربران آنجا دست نمی‌خورند
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmPanel(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              مطمئنید می‌خواهید پنل <strong>{confirmPanel.name}</strong> را حذف کنید؟
              <br />
              <span className="muted" style={{ fontSize: "0.9rem" }} dir="ltr">
                {confirmPanel.base_url}
              </span>
            </p>
            {remove.isError && <p className="error">{(remove.error as Error).message}</p>}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={remove.isPending}
                onClick={() => setConfirmPanel(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirmPanel.id)}
              >
                {remove.isPending ? "در حال حذف…" : "بله، حذف از سایت"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {open && (
        <div className="modal-backdrop" onClick={resetModal}>
          <div className="modal modal-lg panel-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>{isEdit ? "ویرایش پنل" : "افزودن پنل"}</h3>
                <p className="modal-sub">
                  {isEdit
                    ? "تغییرات فقط در صورت اتصال موفق ذخیره می‌شود"
                    : "فقط پس از اتصال واقعی به پنل ریموت ذخیره می‌شود"}
                </p>
              </div>
              <button type="button" className="modal-close" onClick={resetModal} aria-label="بستن">
                ×
              </button>
            </header>

            <form onSubmit={onSubmit} className="panel-form">
              <section className="panel-section">
                <h4>مشخصات</h4>
                <div className="form-grid">
                  <div className="field">
                    <label>نام</label>
                    <input
                      required
                      value={form.name}
                      onChange={(e) => setForm({ ...form, name: e.target.value })}
                      placeholder="مثلاً سنایی اصلی"
                    />
                  </div>
                  <div className="field">
                    <label>نوع</label>
                    <Select
                      value={form.type}
                      disabled={isEdit}
                      options={TYPES.map((t) => ({
                        value: t.value,
                        label: t.label,
                        hint: t.hint,
                      }))}
                      onChange={(type) =>
                        setForm({ ...form, type, api_token: "" })
                      }
                    />
                  </div>
                </div>
                {typeMeta && <p className="field-hint">{typeMeta.hint}</p>}
              </section>

              <section className="panel-section">
                <h4>اتصال</h4>
                <div className="field">
                  <label>آدرس پایه (Base URL)</label>
                  <input
                    required
                    placeholder={
                      isSanaei
                        ? "https://ip:2053/path"
                        : "https://panel.example.com:8443  (بدون /dashboard)"
                    }
                    value={form.base_url}
                    onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                    dir="ltr"
                  />
                </div>
                <div className="form-grid">
                  <div className="field">
                    <label>نام کاربری ادمین *</label>
                    <input
                      required
                      value={form.username}
                      onChange={(e) => setForm({ ...form, username: e.target.value })}
                      dir="ltr"
                    />
                  </div>
                  <div className="field">
                    <label>{isEdit ? "رمز عبور (خالی = بدون تغییر)" : "رمز عبور *"}</label>
                    <input
                      required={!isEdit}
                      type="password"
                      value={form.password}
                      onChange={(e) => setForm({ ...form, password: e.target.value })}
                      dir="ltr"
                      autoComplete="new-password"
                    />
                  </div>
                </div>

                {isSanaei && (
                  <div className="field">
                    <label>{isEdit ? "API Token (خالی = بدون تغییر)" : "API Token *"}</label>
                    <input
                      required={!isEdit}
                      placeholder="Settings → Security → API Token"
                      value={form.api_token}
                      onChange={(e) => setForm({ ...form, api_token: e.target.value })}
                      dir="ltr"
                    />
                  </div>
                )}
              </section>

              {isSanaei && (
                <section className="panel-section">
                  <div className="inbound-head">
                    <div>
                      <h4>اینباندها</h4>
                      <p className="field-hint">
                        روی هر اینباند کلیک کنید تا اضافه شود. خالی = همه اینباندها
                      </p>
                    </div>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() => void loadInbounds()}
                      disabled={inboundsLoading}
                    >
                      {inboundsLoading ? "…" : inbounds.length ? "تازه‌سازی" : "بارگذاری لیست"}
                    </button>
                  </div>

                  {selectedDetails.length > 0 && (
                    <div className="inbound-picked">
                      <span className="inbound-picked-label">انتخاب‌شده:</span>
                      <div className="inbound-chips">
                        {selectedDetails.map((ib) => (
                          <button
                            key={ib.id}
                            type="button"
                            className="inbound-chip on"
                            onClick={() => removeSelected(ib.id)}
                            title="حذف از انتخاب"
                          >
                            <span>{inboundLabel(ib)}</span>
                            <i aria-hidden>×</i>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {inboundsErr && <p className="error">{inboundsErr}</p>}

                  {inboundsLoading && <p className="muted">در حال دریافت اینباندها…</p>}

                  {!inboundsLoading && inbounds.length > 0 && (
                    <div className="inbound-list">
                      {inbounds.map((ib) => {
                        const on = selectedSet.has(ib.id);
                        return (
                          <button
                            key={ib.id}
                            type="button"
                            className={`inbound-item${on ? " selected" : ""}${!ib.enable ? " off" : ""}`}
                            onClick={() => toggleInbound(ib.id)}
                          >
                            <span className="inbound-check" aria-hidden>
                              {on ? "✓" : "+"}
                            </span>
                            <span className="inbound-body">
                              <strong>{ib.remark?.trim() || `Inbound #${ib.id}`}</strong>
                              <small>
                                ID {ib.id}
                                {ib.protocol ? ` · ${ib.protocol}` : ""}
                                {ib.port != null ? ` · پورت ${ib.port}` : ""}
                                {!ib.enable ? " · غیرفعال" : ""}
                              </small>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {!inboundsLoading && !inbounds.length && !inboundsErr && (
                    <p className="muted inbound-empty">
                      {isEdit
                        ? "لیست در حال بارگذاری است یا پنل اینباند ندارد"
                        : "پس از وارد کردن آدرس و Token، «بارگذاری لیست» را بزنید"}
                    </p>
                  )}
                </section>
              )}

              {isPasarguard && (
                <section className="panel-section">
                  <div className="inbound-head">
                    <div>
                      <h4>گروه‌های ریموت</h4>
                      <p className="field-hint">
                        روی هر گروه کلیک کنید تا اضافه شود. خالی = همه گروه‌های فعال
                      </p>
                    </div>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() => void loadRemoteGroups()}
                      disabled={groupsLoading}
                    >
                      {groupsLoading ? "…" : remoteGroups.length ? "تازه‌سازی" : "بارگذاری لیست"}
                    </button>
                  </div>

                  {selectedGroupDetails.length > 0 && (
                    <div className="inbound-picked">
                      <span className="inbound-picked-label">انتخاب‌شده:</span>
                      <div className="inbound-chips">
                        {selectedGroupDetails.map((g) => (
                          <button
                            key={g.id}
                            type="button"
                            className="inbound-chip on"
                            onClick={() => removeSelectedGroup(g.id)}
                            title="حذف از انتخاب"
                          >
                            <span>
                              {g.name} · #{g.id}
                            </span>
                            <i aria-hidden>×</i>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {groupsErr && <p className="error">{groupsErr}</p>}
                  {groupsLoading && <p className="muted">در حال دریافت گروه‌ها…</p>}

                  {!groupsLoading && remoteGroups.length > 0 && (
                    <div className="inbound-list">
                      {remoteGroups.map((g) => {
                        const on = selectedGroupSet.has(g.id);
                        return (
                          <button
                            key={g.id}
                            type="button"
                            className={`inbound-item${on ? " selected" : ""}${g.is_disabled ? " off" : ""}`}
                            onClick={() => toggleGroup(g.id)}
                          >
                            <span className="inbound-check" aria-hidden>
                              {on ? "✓" : "+"}
                            </span>
                            <span className="inbound-body">
                              <strong>{g.name}</strong>
                              <small>
                                ID {g.id}
                                {g.is_disabled ? " · غیرفعال" : ""}
                              </small>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {!groupsLoading && !remoteGroups.length && !groupsErr && (
                    <p className="muted inbound-empty">
                      {isEdit
                        ? "لیست در حال بارگذاری است یا پنل گروهی ندارد"
                        : "پس از وارد کردن آدرس و یوزر/پسورد، «بارگذاری لیست» را بزنید"}
                    </p>
                  )}
                </section>
              )}

              {isRebecca && (
                <section className="panel-section">
                  <div className="inbound-head">
                    <div>
                      <h4>ربکا</h4>
                      <p className="field-hint">
                        در ربکا انتخاب پروتکل/اینباند وجود ندارد. با ساخت کاربر، کانفیگ‌ها از
                        سرویس‌های تعریف‌شده در داشبورد ربکا می‌آیند. سرویس را هنگام ساخت گروه
                        انتخاب کنید (خالی = اولین سرویس).
                      </p>
                    </div>
                  </div>
                </section>
              )}

              {isMarzban && (
                <section className="panel-section">
                  <div className="inbound-head">
                    <div>
                      <h4>پروتکل‌ها (مرزبان)</h4>
                      <p className="field-hint">
                        روی هر پروتکل کلیک کنید تا اضافه شود. خالی = همه پروتکل‌ها
                      </p>
                    </div>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() => void loadMarzProtocols()}
                      disabled={marzLoading}
                    >
                      {marzLoading ? "…" : marzProtocols.length ? "تازه‌سازی" : "بارگذاری لیست"}
                    </button>
                  </div>

                  {selectedProtocolDetails.length > 0 && (
                    <div className="inbound-picked">
                      <span className="inbound-picked-label">انتخاب‌شده:</span>
                      <div className="inbound-chips">
                        {selectedProtocolDetails.map((p) => (
                          <button
                            key={p.protocol}
                            type="button"
                            className="inbound-chip on"
                            onClick={() => removeSelectedProtocol(p.protocol)}
                            title="حذف از انتخاب"
                          >
                            <span>
                              {p.protocol}
                              {p.inbound_count ? ` · ${p.inbound_count} inbound` : ""}
                            </span>
                            <i aria-hidden>×</i>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {marzErr && <p className="error">{marzErr}</p>}
                  {marzLoading && <p className="muted">در حال دریافت پروتکل‌ها…</p>}

                  {!marzLoading && marzProtocols.length > 0 && (
                    <div className="inbound-list">
                      {marzProtocols.map((p) => {
                        const on = selectedProtocolSet.has(p.protocol);
                        return (
                          <button
                            key={p.protocol}
                            type="button"
                            className={`inbound-item${on ? " selected" : ""}`}
                            onClick={() => toggleProtocol(p.protocol)}
                          >
                            <span className="inbound-check" aria-hidden>
                              {on ? "✓" : "+"}
                            </span>
                            <span className="inbound-body">
                              <strong>{p.protocol}</strong>
                              <small>
                                {p.inbound_count
                                  ? `${p.inbound_count} اینباند`
                                  : "بدون اینباند"}
                                {p.tags.length
                                  ? ` · ${p.tags.slice(0, 3).join(", ")}${p.tags.length > 3 ? "…" : ""}`
                                  : ""}
                              </small>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {!marzLoading && !marzProtocols.length && !marzErr && (
                    <p className="muted inbound-empty">
                      {isEdit
                        ? "لیست در حال بارگذاری است یا پنل پروتکلی ندارد"
                        : "پس از وارد کردن آدرس و یوزر/پسورد، «بارگذاری لیست» را بزنید"}
                    </p>
                  )}
                </section>
              )}

              {save.isError && <p className="error">{(save.error as Error).message}</p>}

              <footer className="modal-foot">
                <button type="button" className="btn ghost" onClick={resetModal}>
                  انصراف
                </button>
                <button className="btn" disabled={save.isPending}>
                  {save.isPending
                    ? "در حال تست اتصال…"
                    : isEdit
                      ? "ذخیره تغییرات"
                      : "تست و افزودن"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
