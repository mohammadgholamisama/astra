import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRightLeft,
  FolderPlus,
  Layers,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { api, Group, GroupCategory, Panel, PanelType } from "../lib/api";
import { toFaDigits } from "../lib/format";

/** Active tab: null = default «همه گروه‌ها» (uncategorized) */
type CatTab = null | number;

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

/** Parse Astra usage coefficient; accepts 1.33 / 1,33 / فارسی digits. */
function parseCoeff(raw: unknown, fallback = 1): number {
  if (typeof raw === "number") {
    return !Number.isNaN(raw) && raw >= 0.01 ? raw : fallback;
  }
  let s = String(raw ?? "").trim();
  s = s
    .replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
    .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString())
    .replace(/,/g, ".");
  if (!s || s === ".") return fallback;
  const n = Number(s);
  if (Number.isNaN(n) || n < 0.01) return fallback;
  return Math.min(1000, n);
}

function formatCoeffInput(c: number): string {
  if (!Number.isFinite(c)) return "1";
  if (c === Math.floor(c)) return String(c);
  return String(Math.round(c * 10000) / 10000);
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
  cfg.coefficient = parseCoeff(pick.coefficient, 1);
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
  pick.coefficient = parseCoeff(cfg.coefficient, 1);
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
  const [activeTab, setActiveTab] = useState<CatTab>(null);
  const [moveGroup, setMoveGroup] = useState<Group | null>(null);
  const [catModal, setCatModal] = useState<"create" | { edit: GroupCategory } | null>(null);
  const [catName, setCatName] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [configTag, setConfigTag] = useState("");
  const [picks, setPicks] = useState<PanelPick[]>([]);
  const [catalogs, setCatalogs] = useState<Record<number, PanelCatalog>>({});
  const [confirmGroup, setConfirmGroup] = useState<Group | null>(null);
  /** Draft text while typing coefficient (keeps "1." / "1,33" mid-edit). */
  const [coeffDrafts, setCoeffDrafts] = useState<Record<number, string>>({});
  const loadingRef = useRef<Set<number>>(new Set());
  const attemptedRef = useRef<Set<number>>(new Set());
  const seededRef = useRef<Set<number>>(new Set());
  const isEdit = editingId != null;

  const { data: groups = [], isLoading } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<Group[]>("/api/groups"),
  });
  const { data: categories = [] } = useQuery({
    queryKey: ["group-categories"],
    queryFn: () => api<GroupCategory[]>("/api/group-categories"),
  });
  const { data: panels = [] } = useQuery({
    queryKey: ["panels"],
    queryFn: () => api<Panel[]>("/api/admin/panels"),
  });

  const panelMap = useMemo(() => Object.fromEntries(panels.map((p) => [p.id, p])), [panels]);
  const selectedIds = useMemo(() => new Set(picks.map((p) => p.panel_id)), [picks]);

  const uncategorizedCount = useMemo(
    () => groups.filter((g) => g.category_id == null).length,
    [groups],
  );
  const visibleGroups = useMemo(() => {
    if (activeTab == null) return groups.filter((g) => g.category_id == null);
    return groups.filter((g) => g.category_id === activeTab);
  }, [groups, activeTab]);

  // If active category was deleted, fall back to default tab
  useEffect(() => {
    if (activeTab == null) return;
    if (!categories.some((c) => c.id === activeTab)) setActiveTab(null);
  }, [categories, activeTab]);

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
      // Finalize any mid-edit coefficient drafts (e.g. "1,33") before payload
      const finalized = picks.map((pick) => {
        const draft = coeffDrafts[pick.panel_id];
        if (draft === undefined) return pick;
        return { ...pick, coefficient: parseCoeff(draft, pick.coefficient) };
      });
      const bodyPanels = finalized.map((pick, i) => {
        const panel = panelMap[pick.panel_id];
        return {
          panel_id: pick.panel_id,
          config: panel ? pickToConfig(pick, panel.type) : {},
          priority: finalized.length - i,
        };
      });
      const payload: Record<string, unknown> = {
        name,
        description: description || null,
        config_tag: configTag.trim(),
        notices: [] as string[],
        panels: bodyPanels,
      };
      if (editingId != null) {
        return api<Group>(`/api/group/${editingId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
      }
      // New groups land in the active tab (null = «همه گروه‌ها»)
      if (activeTab != null) payload.category_id = activeTab;
      return api<Group>("/api/group", {
        method: "POST",
        body: JSON.stringify(payload),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["group-categories"] });
      // Panel add/remove resyncs remotes + subs in the background
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["configs"] });
      resetModal();
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/group/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["group-categories"] });
      setConfirmGroup(null);
    },
  });

  const moveMut = useMutation({
    mutationFn: ({ id, category_id }: { id: number; category_id: number | null }) =>
      api<Group>(`/api/group/${id}/move`, {
        method: "PUT",
        body: JSON.stringify({ category_id }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["group-categories"] });
      setMoveGroup(null);
    },
  });

  const saveCategory = useMutation({
    mutationFn: async () => {
      const n = catName.trim();
      if (!n) throw new Error("نام دسته را وارد کنید");
      if (catModal === "create") {
        return api<GroupCategory>("/api/group-categories", {
          method: "POST",
          body: JSON.stringify({ name: n }),
        });
      }
      if (catModal && typeof catModal === "object" && "edit" in catModal) {
        return api<GroupCategory>(`/api/group-categories/${catModal.edit.id}`, {
          method: "PUT",
          body: JSON.stringify({ name: n }),
        });
      }
      throw new Error("invalid");
    },
    onSuccess: (cat) => {
      qc.invalidateQueries({ queryKey: ["group-categories"] });
      setCatModal(null);
      setCatName("");
      if (catModal === "create") setActiveTab(cat.id);
    },
  });

  const deleteCategory = useMutation({
    mutationFn: (id: number) => api(`/api/group-categories/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["group-categories"] });
      qc.invalidateQueries({ queryKey: ["groups"] });
      setActiveTab(null);
      setCatModal(null);
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
    setConfigTag("");
    setPicks([]);
    setCatalogs({});
    setCoeffDrafts({});
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
    setConfigTag((group.config_tag || "").trim());
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
    setCoeffDrafts({});
    setOpen(true);
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

  const activeCat = activeTab != null ? categories.find((c) => c.id === activeTab) : null;

  return (
    <div className="admin-page">
      <h1 className="page-title">گروه‌ها</h1>
      <p className="page-sub">
        گروه‌ها را در دسته‌ها (مثل مستقیم، تانل، گیمینگ) مرتب کنید. گروه جدید پیش‌فرض در «همه گروه‌ها»
        می‌آید مگر روی تب دسته دیگری باشید. مصرف Astra = جمع (مصرف خام × ضریب هر گروه).
      </p>

      <div className="toolbar">
        <span className="muted">
          {toFaDigits(visibleGroups.length)} گروه
          {activeCat ? ` در «${activeCat.name}»` : " بدون دسته"}
          <span className="groups-total-hint"> · کل {toFaDigits(groups.length)}</span>
        </span>
        <button className="btn toolbar-primary" onClick={openCreate}>
          <Plus size={16} />
          گروه جدید
        </button>
      </div>

      <div className="groups-cat-bar" role="tablist" aria-label="دسته‌بندی گروه‌ها">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab == null}
          className={`groups-cat-tab${activeTab == null ? " active" : ""}`}
          onClick={() => setActiveTab(null)}
        >
          همه گروه‌ها
          <span className="groups-cat-count">{toFaDigits(uncategorizedCount)}</span>
        </button>
        {categories.map((c) => (
          <button
            key={c.id}
            type="button"
            role="tab"
            aria-selected={activeTab === c.id}
            className={`groups-cat-tab${activeTab === c.id ? " active" : ""}`}
            onClick={() => setActiveTab(c.id)}
            onDoubleClick={() => {
              setCatName(c.name);
              setCatModal({ edit: c });
            }}
            title="دبل‌کلیک برای ویرایش نام"
          >
            {c.name}
            <span className="groups-cat-count">{toFaDigits(c.groups_count)}</span>
          </button>
        ))}
        <button
          type="button"
          className="groups-cat-add"
          onClick={() => {
            setCatName("");
            setCatModal("create");
          }}
          title="دسته جدید"
        >
          <FolderPlus size={16} />
          <span>دسته جدید</span>
        </button>
        {activeCat && (
          <button
            type="button"
            className="groups-cat-manage"
            onClick={() => {
              setCatName(activeCat.name);
              setCatModal({ edit: activeCat });
            }}
          >
            <Pencil size={14} />
            ویرایش دسته
          </button>
        )}
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
      ) : visibleGroups.length === 0 ? (
        <div className="admin-empty groups-empty-tab">
          <Layers size={32} strokeWidth={1.4} />
          <p>
            {activeTab == null
              ? "گروه بدون دسته‌ای نیست — از دکمه جابه‌جایی گروه را به اینجا برگردانید"
              : `در «${activeCat?.name || "این دسته"}» گروهی نیست`}
          </p>
          <button className="btn" onClick={openCreate}>
            ساخت گروه در این تب
          </button>
        </div>
      ) : (
        <div className="groups-list">
          {visibleGroups.map((g) => (
            <article key={g.id} className={`groups-row${g.is_disabled ? " is-off" : ""}`}>
              <div className="groups-row-main">
                <div className="groups-row-title">
                  <b>{g.name}</b>
                  {g.description ? <span className="groups-row-desc">{g.description}</span> : null}
                </div>
                <div className="groups-row-meta">
                  <span className={`badge ${g.is_disabled ? "err" : "ok"}`}>
                    {g.is_disabled ? "غیرفعال" : "فعال"}
                  </span>
                  <span className="badge muted">{toFaDigits((g.panels || []).length)} پنل</span>
                  {(g.config_tag || "").trim() ? (
                    <span className="badge muted" dir="ltr">
                      {(g.config_tag || "").trim()}
                    </span>
                  ) : null}
                </div>
                <div className="groups-row-chips">
                  {(g.panels || []).map((p) => {
                    const coeff = parseCoeff(
                      (p.config as { coefficient?: number } | undefined)?.coefficient,
                      1
                    );
                    return (
                      <span key={p.panel_id} className="badge muted">
                        {p.panel_name || panelMap[p.panel_id]?.name || `#${p.panel_id}`}
                        {coeff !== 1 ? ` · ×${formatCoeffInput(coeff)}` : ""}
                      </span>
                    );
                  })}
                  {(g.panels || []).length === 0 && (
                    <span className="muted" style={{ fontSize: "0.82rem" }}>
                      بدون پنل
                    </span>
                  )}
                </div>
              </div>
              <div className="groups-row-actions">
                <button
                  type="button"
                  className="btn ghost sm"
                  onClick={() => setMoveGroup(g)}
                  title="جابه‌جایی به دسته دیگر"
                >
                  <ArrowRightLeft size={14} />
                  جابه‌جایی
                </button>
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

              <div className="field">
                <label>متن روی کانفیگ‌های این گروه در ساب (اختیاری)</label>
                <input
                  value={configTag}
                  onChange={(e) => setConfigTag(e.target.value)}
                  placeholder="مثلاً VIP — خالی = بدون متن اضافه"
                  maxLength={24}
                />
                <p className="field-hint" dir="ltr">
                  {configTag.trim()
                    ? `#1. ${configTag.trim()} 2.5G | user`
                    : "#1. 2.5G | user"}
                </p>
                <p className="field-hint">
                  فقط روی کانفیگ‌های پنل‌های همین گروه اعمال می‌شود — از لحظه ذخیره روی همه
                  ساب‌های قبلی کاربران این گروه هم می‌آید (کلاینت ممکن است تا یک ساعت بعد
                  تازه‌سازی کند).
                </p>
              </div>

              <label className="muted" style={{ fontSize: "0.86rem", display: "block", marginBottom: 8 }}>
                انتخاب پنل‌ها و مسیرها
              </label>
              <div className="check-list">
                {panels.map((p) => {
                  const on = selectedIds.has(p.id);
                  const pick = picks.find((x) => x.panel_id === p.id);
                  const cat = catalogs[p.id];
                  const panelOff = !p.is_active;
                  return (
                    <div
                      key={p.id}
                      className={`group-panel-block${on ? " on" : ""}${
                        panelOff ? " is-off" : ""
                      }`}
                    >
                      <label className="check-item">
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={panelOff && !on}
                          onChange={() => togglePanel(p)}
                          title={
                            panelOff && !on
                              ? "این پنل خاموش است — اول از صفحه پنل‌ها روشن کنید"
                              : undefined
                          }
                        />
                        <span>
                          {p.name}{" "}
                          <span className="badge muted">{p.type}</span>
                          {panelOff ? (
                            <span className="badge err">خاموش</span>
                          ) : null}
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
                                type="text"
                                inputMode="decimal"
                                dir="ltr"
                                placeholder="1"
                                autoComplete="off"
                                value={
                                  coeffDrafts[p.id] !== undefined
                                    ? coeffDrafts[p.id]
                                    : formatCoeffInput(pick.coefficient)
                                }
                                onChange={(e) => {
                                  const v = e.target.value;
                                  setCoeffDrafts((prev) => ({ ...prev, [p.id]: v }));
                                  const n = parseCoeff(v, NaN);
                                  if (!Number.isNaN(n)) {
                                    updatePick(p.id, { coefficient: n });
                                  }
                                }}
                                onBlur={() => {
                                  const draft = coeffDrafts[p.id];
                                  const n = parseCoeff(
                                    draft !== undefined ? draft : pick.coefficient,
                                    1
                                  );
                                  updatePick(p.id, { coefficient: n });
                                  setCoeffDrafts((prev) => {
                                    const next = { ...prev };
                                    delete next[p.id];
                                    return next;
                                  });
                                }}
                              />
                              <p className="field-hint" style={{ marginBottom: 0 }}>
                                ۱ = همان حجم دانلود · ۱.۳۳ = مصرف Astra برابر خام × ۱.۳۳ (بیشتر از دانلود).
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
              {isEdit ? (
                <p className="muted" style={{ margin: "8px 0 0", fontSize: "0.85rem" }}>
                  با ذخیره، پنل‌های اضافه‌شده برای همه کاربران این گروه ساخته می‌شوند و
                  پنل‌های حذف‌شده از ساب و از پنل اصلی هم پاک می‌شوند.
                </p>
              ) : null}

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

      {moveGroup && (
        <div
          className="modal-backdrop"
          onClick={() => !moveMut.isPending && setMoveGroup(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>جابه‌جایی گروه</h3>
                <p className="modal-sub">
                  «{moveGroup.name}» را به کدام دسته ببرید؟
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setMoveGroup(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="groups-move-list">
              <button
                type="button"
                className={`groups-move-opt${moveGroup.category_id == null ? " current" : ""}`}
                disabled={moveMut.isPending || moveGroup.category_id == null}
                onClick={() => moveMut.mutate({ id: moveGroup.id, category_id: null })}
              >
                <span>همه گروه‌ها</span>
                <span className="muted">بدون دسته</span>
              </button>
              {categories.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className={`groups-move-opt${moveGroup.category_id === c.id ? " current" : ""}`}
                  disabled={moveMut.isPending || moveGroup.category_id === c.id}
                  onClick={() => moveMut.mutate({ id: moveGroup.id, category_id: c.id })}
                >
                  <span>{c.name}</span>
                  <span className="muted">{toFaDigits(c.groups_count)} گروه</span>
                </button>
              ))}
              {categories.length === 0 && (
                <p className="muted" style={{ margin: "8px 0 0", fontSize: "0.86rem" }}>
                  هنوز دسته‌ای نساخته‌اید — اول «دسته جدید» را بزنید.
                </p>
              )}
            </div>
            {moveMut.isError && (
              <p className="error">{(moveMut.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={moveMut.isPending}
                onClick={() => setMoveGroup(null)}
              >
                بستن
              </button>
            </footer>
          </div>
        </div>
      )}

      {catModal && (
        <div
          className="modal-backdrop"
          onClick={() => !saveCategory.isPending && !deleteCategory.isPending && setCatModal(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>{catModal === "create" ? "دسته جدید" : "ویرایش دسته"}</h3>
                <p className="modal-sub">
                  {catModal === "create"
                    ? "مثلاً مستقیم، تانل، گیمینگ"
                    : "تغییر نام یا حذف دسته — گروه‌هایش به «همه گروه‌ها» برمی‌گردند"}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setCatModal(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                saveCategory.mutate();
              }}
            >
              <div className="field">
                <label>نام دسته</label>
                <input
                  required
                  maxLength={48}
                  value={catName}
                  onChange={(e) => setCatName(e.target.value)}
                  placeholder="مثلاً گیمینگ"
                  autoFocus
                />
              </div>
              {saveCategory.isError && (
                <p className="error">{(saveCategory.error as Error).message}</p>
              )}
              {deleteCategory.isError && (
                <p className="error">{(deleteCategory.error as Error).message}</p>
              )}
              <footer className="modal-foot">
                {catModal !== "create" && typeof catModal === "object" && (
                  <button
                    type="button"
                    className="btn danger"
                    disabled={saveCategory.isPending || deleteCategory.isPending}
                    onClick={() => {
                      if (
                        window.confirm(
                          `فقط دسته «${catModal.edit.name}» حذف شود؟\nگروه‌های داخلش پاک نمی‌شوند و همه به تب «همه گروه‌ها» منتقل می‌شوند.`,
                        )
                      ) {
                        deleteCategory.mutate(catModal.edit.id);
                      }
                    }}
                  >
                    {deleteCategory.isPending ? "…" : "حذف دسته"}
                  </button>
                )}
                <button
                  type="button"
                  className="btn ghost"
                  disabled={saveCategory.isPending || deleteCategory.isPending}
                  onClick={() => setCatModal(null)}
                >
                  انصراف
                </button>
                <button
                  type="submit"
                  className="btn"
                  disabled={saveCategory.isPending || !catName.trim()}
                >
                  {saveCategory.isPending
                    ? "…"
                    : catModal === "create"
                      ? "ساخت"
                      : "ذخیره"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

    </div>
  );
}
