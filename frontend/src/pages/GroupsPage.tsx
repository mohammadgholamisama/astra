import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Layers, Megaphone, Pencil, Plus, Trash2, X } from "lucide-react";
import { api, Group, Panel, PanelType } from "../lib/api";
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
  service_id?: number | null;
  service_name?: string | null;
  is_disabled?: boolean;
};

type PanelPick = {
  panel_id: number;
  inbound_ids: number[];
  group_ids: number[];
  protocols: string[];
  /** Marzban/Rebecca: selected inbound tags per protocol */
  marz_inbounds: Record<string, string[]>;
  /** Optional IP limit for users created on this panel (0 = off) */
  limit_ip: number;
  /** Astra sub usage multiplier for configs from this panel (1 = normal) */
  coefficient: number;
};

type PanelCatalog = {
  loading: boolean;
  error: string;
  inbounds: InboundInfo[];
  groups: RemoteGroupInfo[];
  protocols: MarzProtocolInfo[];
};

const emptyCatalog = (): PanelCatalog => ({
  loading: false,
  error: "",
  inbounds: [],
  groups: [],
  protocols: [],
});

function emptyPick(panelId: number): PanelPick {
  return {
    panel_id: panelId,
    inbound_ids: [],
    group_ids: [],
    protocols: [],
    marz_inbounds: {},
    limit_ip: 0,
    coefficient: 1,
  };
}

function allMarzInbounds(catalog: MarzProtocolInfo[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const p of catalog) {
    out[p.protocol] = [...(p.tags || [])];
  }
  return out;
}

function pickToConfig(pick: PanelPick, type: PanelType): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  if (type === "sanaei" && pick.inbound_ids.length) {
    cfg.inbound_ids = pick.inbound_ids;
  }
  if (type === "pasarguard" && pick.group_ids.length) {
    cfg.remote_group_ids = pick.group_ids;
    cfg.group_ids = pick.group_ids;
  }
  if (type === "marzban" && pick.protocols.length) {
    cfg.protocols = pick.protocols;
    const inbounds: Record<string, string[]> = {};
    for (const protocol of pick.protocols) {
      inbounds[protocol] = [...(pick.marz_inbounds[protocol] || [])];
    }
    cfg.inbounds = inbounds;
  }
  if (type === "rebecca") {
    // Rebecca: one service_id per user (configs come from that service)
    const serviceIds = pick.protocols
      .map((p) => {
        const m = String(p).match(/^service-(\d+)$/i);
        return m ? Number(m[1]) : NaN;
      })
      .filter((n) => !Number.isNaN(n));
    if (serviceIds.length) {
      cfg.service_id = serviceIds[0];
      cfg.service_ids = serviceIds;
    }
  }
  if (pick.limit_ip > 0) {
    cfg.limit_ip = pick.limit_ip;
  }
  const coeff = Number(pick.coefficient);
  cfg.coefficient = !Number.isNaN(coeff) && coeff >= 0.01 ? coeff : 1;
  return cfg;
}

/** Restore PanelPick from saved GroupPanel.config (edit mode). */
function configToPick(
  panelId: number,
  config: Record<string, unknown> | undefined,
  type: PanelType | string | null | undefined
): PanelPick {
  const pick = emptyPick(panelId);
  const cfg = config || {};
  const asNumList = (v: unknown) =>
    Array.isArray(v) ? v.map((x) => Number(x)).filter((n) => !Number.isNaN(n)) : [];
  const asStrList = (v: unknown) => (Array.isArray(v) ? v.map(String) : []);

  pick.inbound_ids = asNumList(cfg.inbound_ids);
  const remoteGroups = cfg.remote_group_ids;
  pick.group_ids = asNumList(
    Array.isArray(remoteGroups) && remoteGroups.length ? remoteGroups : cfg.group_ids
  );

  if (type === "rebecca" || cfg.service_id != null) {
    const sid = Number(cfg.service_id);
    if (!Number.isNaN(sid) && sid > 0) {
      pick.protocols = [`service-${sid}`];
    } else {
      const ids = asNumList(cfg.service_ids);
      pick.protocols = ids.map((id) => `service-${id}`);
    }
  } else {
    pick.protocols = asStrList(cfg.protocols);
    const rawIn = cfg.inbounds;
    if (rawIn && typeof rawIn === "object" && !Array.isArray(rawIn)) {
      const map: Record<string, string[]> = {};
      for (const [proto, tags] of Object.entries(rawIn as Record<string, unknown>)) {
        map[proto] = asStrList(tags);
      }
      pick.marz_inbounds = map;
    }
  }

  const lip = Number(cfg.limit_ip);
  pick.limit_ip = !Number.isNaN(lip) && lip > 0 ? lip : 0;
  const coeff = Number(cfg.coefficient);
  pick.coefficient = !Number.isNaN(coeff) && coeff >= 0.01 ? coeff : 1;
  return pick;
}

function inboundLabel(ib: InboundInfo) {
  const title = ib.remark?.trim() || `Inbound #${ib.id}`;
  const meta = [ib.protocol, ib.port != null ? `:${ib.port}` : ""].filter(Boolean).join(" ");
  return meta ? `${title} (${meta})` : title;
}

function mergeIds(prev: number[], next: number[], reseed: boolean): number[] {
  if (reseed) return next;
  // Edit / refresh: keep prior selection, drop ids that no longer exist
  if (!prev.length) return [];
  return prev.filter((id) => next.includes(id));
}

function mergeProtocols(prev: string[], next: string[], reseed: boolean): string[] {
  if (reseed) return next;
  if (!prev.length) return [];
  return prev.filter((p) => next.includes(p));
}

export default function GroupsPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [notices, setNotices] = useState<string[]>([]);
  const [noticeDraft, setNoticeDraft] = useState("");
  const [picks, setPicks] = useState<PanelPick[]>([]);
  const [catalogs, setCatalogs] = useState<Record<number, PanelCatalog>>({});
  const [confirmGroup, setConfirmGroup] = useState<Group | null>(null);
  const loadingRef = useRef<Set<number>>(new Set());
  const attemptedRef = useRef<Set<number>>(new Set());
  const seededRef = useRef<Set<number>>(new Set());
  const isEdit = editingId != null;

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<Group[]>("/api/groups"),
  });
  const { data: panels = [] } = useQuery({
    queryKey: ["panels"],
    queryFn: () => api<Panel[]>("/api/admin/panels"),
  });

  const panelMap = useMemo(() => Object.fromEntries(panels.map((p) => [p.id, p])), [panels]);
  const selectedIds = useMemo(() => new Set(picks.map((p) => p.panel_id)), [picks]);

  const loadCatalog = useCallback(async (panel: Panel, force = false) => {
    if (loadingRef.current.has(panel.id)) return;
    if (!force && attemptedRef.current.has(panel.id)) return;

    loadingRef.current.add(panel.id);
    const firstTime = !seededRef.current.has(panel.id);
    setCatalogs((prev) => ({
      ...prev,
      [panel.id]: { ...(prev[panel.id] || emptyCatalog()), loading: true, error: "" },
    }));

    try {
      if (panel.type === "sanaei") {
        const list = await api<InboundInfo[]>(`/api/admin/panels/${panel.id}/inbounds`);
        const ids = list.map((x) => x.id);
        seededRef.current.add(panel.id);
        setCatalogs((prev) => ({
          ...prev,
          [panel.id]: { ...emptyCatalog(), inbounds: list },
        }));
        setPicks((prev) =>
          prev.map((p) =>
            p.panel_id === panel.id
              ? { ...p, inbound_ids: mergeIds(p.inbound_ids, ids, firstTime) }
              : p
          )
        );
      } else if (panel.type === "pasarguard") {
        const list = await api<RemoteGroupInfo[]>(`/api/admin/panels/${panel.id}/groups`);
        const ids = list.map((x) => x.id);
        seededRef.current.add(panel.id);
        setCatalogs((prev) => ({
          ...prev,
          [panel.id]: { ...emptyCatalog(), groups: list },
        }));
        setPicks((prev) =>
          prev.map((p) =>
            p.panel_id === panel.id
              ? { ...p, group_ids: mergeIds(p.group_ids, ids, firstTime) }
              : p
          )
        );
      } else if (panel.type === "marzban") {
        const list = await api<MarzProtocolInfo[]>(
          `/api/admin/panels/${panel.id}/marz-protocols`
        );
        const names = list.map((x) => x.protocol);
        seededRef.current.add(panel.id);
        setCatalogs((prev) => ({
          ...prev,
          [panel.id]: { ...emptyCatalog(), protocols: list },
        }));
        const allIb = allMarzInbounds(list);
        setPicks((prev) =>
          prev.map((p) => {
            if (p.panel_id !== panel.id) return p;
            const protocols = mergeProtocols(p.protocols, names, firstTime);
            const marz_inbounds: Record<string, string[]> = {};
            for (const proto of protocols) {
              const avail = allIb[proto] || [];
              if (firstTime || !p.marz_inbounds[proto]?.length) {
                marz_inbounds[proto] = [...avail];
              } else {
                const keep = p.marz_inbounds[proto].filter((t) => avail.includes(t));
                const fresh = avail.filter((t) => !keep.includes(t));
                marz_inbounds[proto] = keep.length ? [...keep, ...fresh] : [...avail];
              }
            }
            return { ...p, protocols, marz_inbounds };
          })
        );
      } else if (panel.type === "rebecca") {
        const list = await api<MarzProtocolInfo[]>(
          `/api/admin/panels/${panel.id}/marz-protocols`
        );
        seededRef.current.add(panel.id);
        setCatalogs((prev) => ({
          ...prev,
          [panel.id]: { ...emptyCatalog(), protocols: list },
        }));
        // Default: first service (or keep prior valid selection). Empty = auto first on create.
        setPicks((prev) =>
          prev.map((p) => {
            if (p.panel_id !== panel.id) return p;
            const names = list.map((x) => x.protocol);
            let protocols = p.protocols.filter((x) => names.includes(x));
            if (firstTime || !protocols.length) {
              protocols = names.length ? [names[0]] : [];
            } else if (protocols.length > 1) {
              protocols = [protocols[0]];
            }
            const marz_inbounds: Record<string, string[]> = {};
            for (const proto of protocols) {
              const row = list.find((x) => x.protocol === proto);
              marz_inbounds[proto] = [...(row?.tags || [])];
            }
            return { ...p, protocols, marz_inbounds };
          })
        );
      } else {
        seededRef.current.add(panel.id);
        setCatalogs((prev) => ({
          ...prev,
          [panel.id]: emptyCatalog(),
        }));
      }
    } catch (e) {
      setCatalogs((prev) => ({
        ...prev,
        [panel.id]: {
          ...(prev[panel.id] || emptyCatalog()),
          loading: false,
          error: e instanceof Error ? e.message : "خطا در دریافت لیست",
        },
      }));
    } finally {
      attemptedRef.current.add(panel.id);
      loadingRef.current.delete(panel.id);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    for (const pick of picks) {
      const panel = panelMap[pick.panel_id];
      if (!panel) continue;
      if (!attemptedRef.current.has(panel.id) && !loadingRef.current.has(panel.id)) {
        void loadCatalog(panel);
      }
    }
  }, [open, picks, panelMap, loadCatalog]);

  const save = useMutation({
    mutationFn: () => {
      const bodyPanels = picks.map((pick, i) => {
        const panel = panelMap[pick.panel_id];
        return {
          panel_id: pick.panel_id,
          config: panel ? pickToConfig(pick, panel.type) : {},
          priority: picks.length - i,
        };
      });
      const payload = {
        name,
        description: description || null,
        notices: notices.map((t) => t.trim()).filter(Boolean).slice(0, 10),
        panels: bodyPanels,
      };
      if (editingId != null) {
        return api<Group>(`/api/group/${editingId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      }
      return api<Group>("/api/group", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      resetModal();
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/group/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      setConfirmGroup(null);
    },
  });

  function togglePanel(panel: Panel) {
    setPicks((prev) => {
      if (prev.some((p) => p.panel_id === panel.id)) {
        seededRef.current.delete(panel.id);
        attemptedRef.current.delete(panel.id);
        loadingRef.current.delete(panel.id);
        setCatalogs((c) => {
          const next = { ...c };
          delete next[panel.id];
          return next;
        });
        return prev.filter((p) => p.panel_id !== panel.id);
      }
      seededRef.current.delete(panel.id);
      attemptedRef.current.delete(panel.id);
      return [...prev, emptyPick(panel.id)];
    });
  }

  function updatePick(panelId: number, patch: Partial<PanelPick>) {
    setPicks((prev) => prev.map((p) => (p.panel_id === panelId ? { ...p, ...patch } : p)));
  }

  function toggleInbound(panelId: number, id: number) {
    setPicks((prev) =>
      prev.map((p) => {
        if (p.panel_id !== panelId) return p;
        const on = p.inbound_ids.includes(id);
        return {
          ...p,
          inbound_ids: on ? p.inbound_ids.filter((x) => x !== id) : [...p.inbound_ids, id],
        };
      })
    );
  }

  function toggleGroupId(panelId: number, id: number) {
    setPicks((prev) =>
      prev.map((p) => {
        if (p.panel_id !== panelId) return p;
        const on = p.group_ids.includes(id);
        return {
          ...p,
          group_ids: on ? p.group_ids.filter((x) => x !== id) : [...p.group_ids, id],
        };
      })
    );
  }

  function toggleProtocol(panelId: number, protocol: string, allTags: string[]) {
    setPicks((prev) =>
      prev.map((p) => {
        if (p.panel_id !== panelId) return p;
        const on = p.protocols.includes(protocol);
        if (on) {
          const marz_inbounds = { ...p.marz_inbounds };
          delete marz_inbounds[protocol];
          return {
            ...p,
            protocols: p.protocols.filter((x) => x !== protocol),
            marz_inbounds,
          };
        }
        return {
          ...p,
          protocols: [...p.protocols, protocol],
          marz_inbounds: { ...p.marz_inbounds, [protocol]: [...allTags] },
        };
      })
    );
  }

  function toggleMarzTag(panelId: number, protocol: string, tag: string) {
    setPicks((prev) =>
      prev.map((p) => {
        if (p.panel_id !== panelId) return p;
        const current = p.marz_inbounds[protocol] || [];
        const on = current.includes(tag);
        const nextTags = on ? current.filter((t) => t !== tag) : [...current, tag];
        const marz_inbounds = { ...p.marz_inbounds, [protocol]: nextTags };
        let protocols = p.protocols;
        if (!protocols.includes(protocol)) {
          protocols = [...protocols, protocol];
        }
        return { ...p, protocols, marz_inbounds };
      })
    );
  }

  function selectAll(panel: Panel) {
    const cat = catalogs[panel.id];
    if (!cat) return;
    if (panel.type === "sanaei") {
      updatePick(panel.id, { inbound_ids: cat.inbounds.map((x) => x.id) });
    } else if (panel.type === "pasarguard") {
      updatePick(panel.id, { group_ids: cat.groups.map((x) => x.id) });
    } else if (panel.type === "marzban") {
      updatePick(panel.id, {
        protocols: cat.protocols.map((x) => x.protocol),
        marz_inbounds: allMarzInbounds(cat.protocols),
      });
    } else if (panel.type === "rebecca") {
      const first = cat.protocols[0];
      if (!first) {
        updatePick(panel.id, { protocols: [], marz_inbounds: {} });
      } else {
        updatePick(panel.id, {
          protocols: [first.protocol],
          marz_inbounds: { [first.protocol]: [...(first.tags || [])] },
        });
      }
    }
  }

  function clearAll(panel: Panel) {
    if (panel.type === "sanaei") updatePick(panel.id, { inbound_ids: [] });
    else if (panel.type === "pasarguard") updatePick(panel.id, { group_ids: [] });
    else if (panel.type === "marzban" || panel.type === "rebecca") {
      updatePick(panel.id, { protocols: [], marz_inbounds: {} });
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!picks.length) return;
    save.mutate();
  }

  function resetModal() {
    setOpen(false);
    setEditingId(null);
    setName("");
    setDescription("");
    setNotices([]);
    setNoticeDraft("");
    setPicks([]);
    setCatalogs({});
    seededRef.current = new Set();
    attemptedRef.current = new Set();
    loadingRef.current = new Set();
  }

  function openCreate() {
    resetModal();
    setOpen(true);
  }

  function openEdit(group: Group) {
    setEditingId(group.id);
    setName(group.name);
    setDescription(group.description || "");
    setNotices([...(group.notices || [])].filter(Boolean).slice(0, 10));
    setNoticeDraft("");
    setCatalogs({});
    attemptedRef.current = new Set();
    loadingRef.current = new Set();
    const nextPicks: PanelPick[] = [];
    const seeded = new Set<number>();
    for (const gp of group.panels || []) {
      const panel = panelMap[gp.panel_id];
      const type = panel?.type || (gp.panel_type as PanelType | undefined);
      nextPicks.push(configToPick(gp.panel_id, gp.config || {}, type));
      // Prevent catalog load from reseeding & wiping saved coeffs / selections
      seeded.add(gp.panel_id);
    }
    seededRef.current = seeded;
    setPicks(nextPicks);
    setOpen(true);
  }

  function addNotice() {
    const text = noticeDraft.trim();
    if (!text) return;
    if (notices.includes(text)) {
      setNoticeDraft("");
      return;
    }
    if (notices.length >= 10) return;
    setNotices((prev) => [...prev, text.slice(0, 200)]);
    setNoticeDraft("");
  }

  function removeNotice(idx: number) {
    setNotices((prev) => prev.filter((_, i) => i !== idx));
  }

  function selectionSummary(panel: Panel, pick: PanelPick, cat?: PanelCatalog) {
    if (!cat || cat.loading) return "در حال دریافت…";
    if (cat.error) return "خطا در دریافت";
    if (panel.type === "sanaei") return `${pick.inbound_ids.length} از ${cat.inbounds.length} اینباند`;
    if (panel.type === "pasarguard") return `${pick.group_ids.length} از ${cat.groups.length} گروه`;
    if (panel.type === "marzban") {
      return `${pick.protocols.length} از ${cat.protocols.length} پروتکل`;
    }
    if (panel.type === "rebecca") {
      return pick.protocols.length
        ? `${pick.protocols.length} از ${cat.protocols.length} سرویس`
        : `خودکار · ${cat.protocols.length} سرویس`;
    }
    return "";
  }

  return (
    <div className="admin-page">
      <h1 className="page-title">گروه‌ها</h1>
      <p className="page-sub">
        کاربر روی همه پنل‌های گروه ساخته می‌شود. سقف حجم بین پنل‌ها مشترک است. ضریب از لحظهٔ تغییر به بعد
        اعمال می‌شود (مثلاً ۲ = مصرف دوبرابر در Astra). اگر سهم Astra زودتر تمام شود، کانفیگ روی پنل اصلی
        خاموش می‌شود.
      </p>

      <div className="toolbar">
        <span className="muted">{toFaDigits(groups.length)} گروه</span>
        <button className="btn toolbar-primary" onClick={openCreate}>
          <Plus size={16} />
          گروه جدید
        </button>
      </div>

      {isLoading ? (
        <div className="admin-skel">
          <div className="admin-skel-card" />
          <div className="admin-skel-card" />
        </div>
      ) : groups.length === 0 ? (
        <div className="admin-empty">
          <Layers size={36} strokeWidth={1.4} />
          <p>گروهی تعریف نشده</p>
          <button className="btn" onClick={openCreate}>
            ساخت اولین گروه
          </button>
        </div>
      ) : (
        <div className="admin-grid">
          {groups.map((g) => (
            <article key={g.id} className={`admin-card ${g.is_disabled ? "is-off" : ""}`}>
              <div className="admin-card-top">
                <div className="admin-card-id">
                  <b>{g.name}</b>
                  {g.description && <p className="admin-card-desc">{g.description}</p>}
                  <div className="admin-card-tags">
                    <span className={`badge ${g.is_disabled ? "err" : "ok"}`}>
                      {g.is_disabled ? "غیرفعال" : "فعال"}
                    </span>
                    <span className="badge muted">
                      {toFaDigits((g.panels || []).length)} پنل
                    </span>
                    {(g.notices || []).length > 0 && (
                      <span className="badge warn">
                        <Megaphone size={11} style={{ marginInlineEnd: 4, verticalAlign: "-1px" }} />
                        {toFaDigits((g.notices || []).length)} اطلاع
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <div className="admin-card-chips">
                {(g.panels || []).map((p) => {
                  const coeff = Number(
                    (p.config as { coefficient?: number } | undefined)?.coefficient ?? 1
                  );
                  return (
                    <span key={p.panel_id} className="badge muted">
                      {p.panel_name || panelMap[p.panel_id]?.name || `#${p.panel_id}`}
                      {coeff !== 1 ? ` · ×${coeff}` : ""}
                    </span>
                  );
                })}
                {(g.panels || []).length === 0 && (
                  <span className="muted" style={{ fontSize: "0.82rem" }}>
                    بدون پنل
                  </span>
                )}
              </div>
              <div className="admin-card-actions">
                <button type="button" className="btn ghost sm" onClick={() => openEdit(g)}>
                  <Pencil size={14} />
                  ویرایش
                </button>
                <button
                  type="button"
                  className="btn danger sm"
                  onClick={() => setConfirmGroup(g)}
                >
                  <Trash2 size={14} />
                  حذف
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {open && (
        <div
          className="modal-backdrop"
          onClick={() => !save.isPending && resetModal()}
        >
          <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>{isEdit ? "ویرایش گروه" : "ساخت گروه"}</h3>
                <p className="modal-sub">
                  {isEdit
                    ? "نام، پنل‌ها، ضریب ساب و تنظیمات هر پنل را تغییر دهید"
                    : "پنل را انتخاب کنید؛ لیست اینباند/گروه/پروتکل از پنل گرفته می‌شود و به‌صورت پیش‌فرض همه انتخاب‌اند"}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => !save.isPending && resetModal()}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form onSubmit={onSubmit}>
              <div className="field">
                <label>نام گروه</label>
                <input required minLength={3} value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="field">
                <label>توضیح</label>
                <input value={description} onChange={(e) => setDescription(e.target.value)} />
              </div>

              <div className="field group-notices-field">
                <label>کانفیگ اطلاع‌رسانی (اختیاری)</label>
                <p className="field-hint">
                  متن‌هایی که به‌صورت کانفیگ اول در ساب و لینک‌ها به کاربر نشان داده می‌شوند — اگر
                  لازم نیست خالی بگذارید
                </p>
                {notices.length > 0 && (
                  <ul className="group-notices-list">
                    {notices.map((n, i) => (
                      <li key={`${i}-${n.slice(0, 24)}`}>
                        <span className="group-notice-idx">{toFaDigits(i + 1)}</span>
                        <span className="group-notice-text">{n}</span>
                        <button
                          type="button"
                          className="btn ghost sm icon-only"
                          onClick={() => removeNotice(i)}
                          aria-label="حذف اطلاع"
                          title="حذف"
                        >
                          <X size={14} />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="group-notice-add">
                  <input
                    value={noticeDraft}
                    onChange={(e) => setNoticeDraft(e.target.value)}
                    placeholder="مثلاً: قبل اتصال آپدیت بزنید"
                    maxLength={200}
                    disabled={notices.length >= 10}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        addNotice();
                      }
                    }}
                  />
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={addNotice}
                    disabled={!noticeDraft.trim() || notices.length >= 10}
                  >
                    <Plus size={14} />
                    افزودن
                  </button>
                </div>
              </div>

              <label className="muted" style={{ fontSize: "0.86rem", display: "block", marginBottom: 8 }}>
                انتخاب پنل‌ها و مسیرها
              </label>
              <div className="check-list">
                {panels.map((p) => {
                  const on = selectedIds.has(p.id);
                  const pick = picks.find((x) => x.panel_id === p.id);
                  const cat = catalogs[p.id];
                  return (
                    <div key={p.id} className={`group-panel-block${on ? " on" : ""}`}>
                      <label className="check-item">
                        <input type="checkbox" checked={on} onChange={() => togglePanel(p)} />
                        <span>
                          {p.name} <span className="badge muted">{p.type}</span>
                        </span>
                        {on && pick && (
                          <span className="group-pick-count">
                            {selectionSummary(p, pick, cat)}
                          </span>
                        )}
                      </label>

                      {on && pick && (
                        <div className="group-panel-routes">
                          <div className="inbound-head">
                            <div>
                              <h4>
                                {p.type === "sanaei"
                                  ? "اینباندها"
                                  : p.type === "pasarguard"
                                    ? "گروه‌های ریموت"
                                    : p.type === "rebecca"
                                      ? "سرویس‌ها"
                                      : "پروتکل‌ها"}
                              </h4>
                              <p className="field-hint">
                                {p.type === "rebecca"
                                  ? "یک سرویس انتخاب کنید؛ خالی = اولین سرویس پنل. کانفیگ‌ها از همان سرویس می‌آیند."
                                  : "پیش‌فرض همه انتخاب‌اند — هر کدام را که نمی‌خواهید خاموش کنید"}
                              </p>
                            </div>
                            <div className="group-route-actions">
                              <button
                                type="button"
                                className="btn ghost sm"
                                onClick={() => selectAll(p)}
                                disabled={!!cat?.loading}
                              >
                                همه
                              </button>
                              <button
                                type="button"
                                className="btn ghost sm"
                                onClick={() => clearAll(p)}
                                disabled={!!cat?.loading}
                              >
                                هیچ‌کدام
                              </button>
                              <button
                                type="button"
                                className="btn ghost sm"
                                onClick={() => void loadCatalog(p, true)}
                                disabled={!!cat?.loading}
                              >
                                {cat?.loading ? "…" : "تازه‌سازی"}
                              </button>
                            </div>
                          </div>

                          {cat?.error && (
                            <p className="error">
                              {cat.error}{" "}
                              <button
                                type="button"
                                className="btn ghost sm"
                                onClick={() => void loadCatalog(p, true)}
                              >
                                تلاش دوباره
                              </button>
                            </p>
                          )}
                          {(!cat || cat.loading) && <p className="muted">در حال دریافت از پنل…</p>}

                          {p.type === "sanaei" && cat && !cat.loading && cat.inbounds.length > 0 && (
                            <div className="inbound-list">
                              {cat.inbounds.map((ib) => {
                                const selected = pick.inbound_ids.includes(ib.id);
                                return (
                                  <button
                                    key={ib.id}
                                    type="button"
                                    className={`inbound-item${selected ? " selected" : ""}${!ib.enable ? " off" : ""}`}
                                    onClick={() => toggleInbound(p.id, ib.id)}
                                  >
                                    <span className="inbound-check" aria-hidden>
                                      {selected ? "✓" : ""}
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

                          {p.type === "pasarguard" && cat && !cat.loading && cat.groups.length > 0 && (
                            <div className="inbound-list">
                              {cat.groups.map((g) => {
                                const selected = pick.group_ids.includes(g.id);
                                return (
                                  <button
                                    key={g.id}
                                    type="button"
                                    className={`inbound-item${selected ? " selected" : ""}${g.is_disabled ? " off" : ""}`}
                                    onClick={() => toggleGroupId(p.id, g.id)}
                                  >
                                    <span className="inbound-check" aria-hidden>
                                      {selected ? "✓" : ""}
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

                          {p.type === "rebecca" &&
                            cat &&
                            !cat.loading &&
                            cat.protocols.length > 0 && (
                              <div className="inbound-list">
                                {cat.protocols.map((proto) => {
                                  const selected = pick.protocols.includes(proto.protocol);
                                  const label =
                                    proto.service_name ||
                                    proto.protocol.replace(/^service-/i, "Service ");
                                  return (
                                    <button
                                      key={proto.protocol}
                                      type="button"
                                      className={`inbound-item${selected ? " selected" : ""}${proto.is_disabled ? " off" : ""}`}
                                      onClick={() =>
                                        updatePick(p.id, {
                                          protocols: selected ? [] : [proto.protocol],
                                          marz_inbounds: selected
                                            ? {}
                                            : { [proto.protocol]: [...(proto.tags || [])] },
                                        })
                                      }
                                    >
                                      <span className="inbound-check" aria-hidden>
                                        {selected ? "✓" : ""}
                                      </span>
                                      <span className="inbound-body">
                                        <strong>{label}</strong>
                                        <small dir="ltr">
                                          ID {proto.service_id ?? proto.protocol}
                                          {proto.inbound_count
                                            ? ` · ${proto.inbound_count} host`
                                            : ""}
                                          {proto.is_disabled ? " · مشکل‌دار" : ""}
                                        </small>
                                      </span>
                                    </button>
                                  );
                                })}
                              </div>
                            )}

                          {p.type === "marzban" &&
                            cat &&
                            !cat.loading &&
                            cat.protocols.length > 0 && (
                              <div className="marz-proto-list">
                                {cat.protocols.map((proto) => {
                                  const selected = pick.protocols.includes(proto.protocol);
                                  const selectedTags = pick.marz_inbounds[proto.protocol] || [];
                                  const tags = proto.tags || [];
                                  return (
                                    <div
                                      key={proto.protocol}
                                      className={`marz-proto-block${selected ? " on" : ""}`}
                                    >
                                      <button
                                        type="button"
                                        className={`inbound-item${selected ? " selected" : ""}`}
                                        onClick={() =>
                                          toggleProtocol(p.id, proto.protocol, tags)
                                        }
                                      >
                                        <span className="inbound-check" aria-hidden>
                                          {selected ? "✓" : ""}
                                        </span>
                                        <span className="inbound-body">
                                          <strong dir="ltr">{proto.protocol}</strong>
                                          <small>
                                            {selected
                                              ? `${selectedTags.length} از ${tags.length} اینباند`
                                              : tags.length
                                                ? `${tags.length} inbound`
                                                : "بدون inbound"}
                                          </small>
                                        </span>
                                      </button>
                                      {selected && tags.length > 0 && (
                                        <div className="marz-tag-list">
                                          {tags.map((tag) => {
                                            const tagOn = selectedTags.includes(tag);
                                            return (
                                              <button
                                                key={tag}
                                                type="button"
                                                className={`marz-tag${tagOn ? " on" : ""}`}
                                                dir="ltr"
                                                onClick={() =>
                                                  toggleMarzTag(p.id, proto.protocol, tag)
                                                }
                                              >
                                                <span className="inbound-check" aria-hidden>
                                                  {tagOn ? "✓" : ""}
                                                </span>
                                                {tag}
                                              </button>
                                            );
                                          })}
                                        </div>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}

                          {cat &&
                            !cat.loading &&
                            !cat.error &&
                            ((p.type === "sanaei" && !cat.inbounds.length) ||
                              (p.type === "pasarguard" && !cat.groups.length) ||
                              ((p.type === "marzban" || p.type === "rebecca") &&
                                !cat.protocols.length)) && (
                              <p className="muted inbound-empty">
                                {p.type === "rebecca"
                                  ? "سرویسی در پنل ربکا پیدا نشد — اول در داشبورد ربکا Service بسازید"
                                  : "موردی در این پنل پیدا نشد"}
                              </p>
                            )}

                          {p.type === "sanaei" &&
                            pick.inbound_ids.length > 0 &&
                            cat?.inbounds.length ? (
                            <div className="inbound-picked">
                              <span className="inbound-picked-label">انتخاب‌شده:</span>
                              <div className="inbound-chips">
                                {cat.inbounds
                                  .filter((ib) => pick.inbound_ids.includes(ib.id))
                                  .map((ib) => (
                                    <button
                                      key={ib.id}
                                      type="button"
                                      className="inbound-chip on"
                                      onClick={() => toggleInbound(p.id, ib.id)}
                                    >
                                      <span>{inboundLabel(ib)}</span>
                                      <i aria-hidden>×</i>
                                    </button>
                                  ))}
                              </div>
                            </div>
                          ) : null}

                          <div className="user-create-grid" style={{ marginTop: 12 }}>
                            <div className="field" style={{ marginBottom: 0 }}>
                              <label>ضریب ساب Astra</label>
                              <input
                                type="number"
                                min={0.01}
                                step="any"
                                inputMode="decimal"
                                dir="ltr"
                                placeholder="1"
                                value={pick.coefficient}
                                onChange={(e) => {
                                  const n = Number(e.target.value);
                                  updatePick(p.id, {
                                    coefficient: !Number.isNaN(n) && n >= 0.01 ? n : 1,
                                  });
                                }}
                              />
                              <p className="field-hint" style={{ marginBottom: 0 }}>
                                ۱ = عادی · ۲ = هر ۱ مگ روی پنل = ۲ مگ از سقف Astra (سقف بین پنل‌ها مشترک است)
                              </p>
                            </div>
                            <div className="field" style={{ marginBottom: 0 }}>
                              <label>محدودیت IP (اختیاری)</label>
                              <input
                                type="number"
                                min={0}
                                dir="ltr"
                                placeholder="0 = بدون محدودیت"
                                value={pick.limit_ip || ""}
                                onChange={(e) =>
                                  updatePick(p.id, {
                                    limit_ip: Math.max(0, Number(e.target.value) || 0),
                                  })
                                }
                              />
                              <p className="field-hint" style={{ marginBottom: 0 }}>
                                روی پنل ریموت هنگام ساخت کاربر
                              </p>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
                {panels.length === 0 && <p className="muted">اول یک پنل اضافه کنید</p>}
              </div>

              {save.isError && <p className="error">{(save.error as Error).message}</p>}

              <footer className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={save.isPending}
                  onClick={resetModal}
                >
                  انصراف
                </button>
                <button className="btn" disabled={save.isPending || !picks.length}>
                  {save.isPending ? "در حال ذخیره…" : isEdit ? "ذخیره تغییرات" : "ذخیره گروه"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {confirmGroup && (
        <div
          className="modal-backdrop"
          onClick={() => !remove.isPending && setConfirmGroup(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>تأیید حذف گروه</h3>
                <p className="modal-sub">
                  فقط از سایت Astra حذف می‌شود — پنل‌های ریموت و کانفیگ‌های آنجا دست نمی‌خورند
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmGroup(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              مطمئنید می‌خواهید گروه <strong>{confirmGroup.name}</strong> را حذف کنید؟
              <br />
              <span className="muted" style={{ fontSize: "0.9rem" }}>
                {(confirmGroup.panels || []).length} پنل از این گروه جدا می‌شود. کاربران ساخته‌شده
                روی پنل‌ها باقی می‌مانند.
              </span>
            </p>
            {remove.isError && <p className="error">{(remove.error as Error).message}</p>}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={remove.isPending}
                onClick={() => setConfirmGroup(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirmGroup.id)}
              >
                {remove.isPending ? "در حال حذف…" : "بله، حذف از سایت"}
              </button>
            </footer>
          </div>
        </div>
      )}

    </div>
  );
}
