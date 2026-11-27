import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRightLeft,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FolderPlus,
  Gauge,
  GripVertical,
  ListOrdered,
  Pencil,
  Plus,
  Power,
  Search,
  Server,
  ShieldCheck,
  Timer,
  Trash2,
  Wifi,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import Select from "../components/Select";
import { api, ApiError, Panel, PanelCategory, PanelType } from "../lib/api";
import { toFaDigits } from "../lib/format";

type CatTab = null | number;

function hostFromBaseUrl(url: string): string {
  return url
    .replace(/^https?:\/\//i, "")
    .split("/")[0]
    .split(":")[0]
    .trim()
    .toLowerCase();
}

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
    value: "astra",
    label: "آسترا (اوتباند)",
    hint: "اوتباند شِیر روی نود محلی — مصرف و خاموشی واقعی از طریق Astra",
  },
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
    value: "guard",
    label: "گارد",
    hint: "یوزر/پسورد یا API Key — سرویس‌ها؛ خاموشی حجم از Astra",
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

/** Strip UI tails; keep webBasePath like /moj for 3x-ui */
function normalizeBaseUrl(url: string) {
  let u = url.trim().replace(/\/+$/, "");
  u = u.replace(/\/(panel\/inbounds|panel\/api-docs|panel\/settings|dashboard|admin|panel|ui|app)$/i, "");
  u = u.replace(/\/api(\/admin)?$/i, "");
  return u.replace(/\/+$/, "");
}

const emptyForm = {
  name: "",
  type: "astra" as PanelType,
  base_url: "",
  username: "",
  password: "",
  api_token: "",
  is_active: true,
  public_port: "443",
};

function inboundLabel(ib: InboundInfo) {
  const title = ib.remark?.trim() || `Inbound #${ib.id}`;
  const meta = [ib.protocol, ib.port != null ? `:${ib.port}` : ""].filter(Boolean).join(" ");
  return meta ? `${title} · ${meta}` : title;
}

export default function PanelsPage() {
  const qc = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Panel | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [activeTab, setActiveTab] = useState<CatTab>(null);
  const [search, setSearch] = useState("");
  const [reorderMode, setReorderMode] = useState(false);
  const [reorderIds, setReorderIds] = useState<number[]>([]);
  const [dragOverId, setDragOverId] = useState<number | null>(null);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const dragPanelId = useRef<number | null>(null);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const deepLinkDone = useRef<string | null>(null);
  const [movePanel, setMovePanel] = useState<Panel | null>(null);
  const [catModal, setCatModal] = useState<"create" | { edit: PanelCategory } | null>(
    null
  );
  const [catName, setCatName] = useState("");
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
  const [sslDomain, setSslDomain] = useState("");
  const [sslMsg, setSslMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const typeMeta = useMemo(() => TYPES.find((t) => t.value === form.type), [form.type]);
  const isSanaei = form.type === "sanaei";
  const isPasarguard = form.type === "pasarguard";
  const isMarzban = form.type === "marzban";
  const isRebecca = form.type === "rebecca";
  const isGuard = form.type === "guard";
  const isServicePanel = isRebecca || isGuard;
  const isAstra = form.type === "astra";
  const isEdit = !!editing;

  const { data = [], isLoading } = useQuery({
    queryKey: ["panels"],
    queryFn: () => api<Panel[]>("/api/admin/panels"),
  });
  const { data: categories = [] } = useQuery({
    queryKey: ["panel-categories"],
    queryFn: () => api<PanelCategory[]>("/api/admin/panel-categories"),
  });

  const { data: siteSettings } = useQuery({
    queryKey: ["site-settings"],
    queryFn: () => api<{ ssl_available: boolean }>("/api/admin/settings"),
    staleTime: 60_000,
  });
  const sslAvailable = Boolean(siteSettings?.ssl_available);

  const uncategorizedCount = useMemo(
    () => data.filter((p) => p.category_id == null).length,
    [data]
  );
  const searchQ = search.trim().toLowerCase();
  const visiblePanels = useMemo(() => {
    let list = data;
    if (searchQ) {
      list = data.filter((p) => {
        const typeLabel =
          TYPES.find((t) => t.value === p.type)?.label || p.type || "";
        return (
          p.name.toLowerCase().includes(searchQ) ||
          (p.base_url || "").toLowerCase().includes(searchQ) ||
          String(p.id) === searchQ ||
          typeLabel.toLowerCase().includes(searchQ)
        );
      });
    } else if (activeTab == null) {
      list = data.filter((p) => p.category_id == null);
    } else {
      list = data.filter((p) => p.category_id === activeTab);
    }
    return [...list].sort((a, b) => {
      const sa = a.sort_order ?? 0;
      const sb = b.sort_order ?? 0;
      if (sa !== sb) return sa - sb;
      return a.id - b.id;
    });
  }, [data, activeTab, searchQ]);
  const activeCat = useMemo(
    () => (activeTab == null ? null : categories.find((c) => c.id === activeTab) || null),
    [activeTab, categories]
  );

  useEffect(() => {
    if (activeTab == null) return;
    if (!categories.some((c) => c.id === activeTab)) setActiveTab(null);
  }, [activeTab, categories]);

  useEffect(() => {
    // Leaving a tab cancels unfinished reorder draft
    if (!reorderMode) return;
    setReorderMode(false);
    setReorderIds([]);
    setDragOverId(null);
    setDraggingId(null);
    dragPanelId.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  function beginReorder() {
    if (searchQ) setSearch("");
    // Full tab list — never the search-filtered subset (that broke save)
    const tabIds = [...data]
      .filter((p) =>
        activeTab == null ? p.category_id == null : p.category_id === activeTab
      )
      .sort((a, b) => {
        const sa = a.sort_order ?? 0;
        const sb = b.sort_order ?? 0;
        if (sa !== sb) return sa - sb;
        return a.id - b.id;
      })
      .map((p) => p.id);
    setReorderIds(tabIds);
    setReorderMode(true);
    setDragOverId(null);
    setDraggingId(null);
    dragPanelId.current = null;
  }

  function cancelReorder() {
    setReorderMode(false);
    setReorderIds([]);
    setDragOverId(null);
    setDraggingId(null);
    dragPanelId.current = null;
  }

  function moveReorderId(fromId: number, toId: number) {
    if (fromId === toId) return;
    setReorderIds((prev) => {
      const next = [...prev];
      const from = next.indexOf(fromId);
      const to = next.indexOf(toId);
      if (from < 0 || to < 0) return prev;
      next.splice(from, 1);
      next.splice(to, 0, fromId);
      return next;
    });
  }

  function shiftReorderId(id: number, dir: -1 | 1) {
    setReorderIds((prev) => {
      const from = prev.indexOf(id);
      if (from < 0) return prev;
      const to = from + dir;
      if (to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [row] = next.splice(from, 1);
      next.splice(to, 0, row);
      return next;
    });
  }

  const reorderPanels = useMemo(() => {
    const byId = new Map(data.map((p) => [p.id, p]));
    return reorderIds.map((id) => byId.get(id)).filter(Boolean) as Panel[];
  }, [data, reorderIds]);

  // Deep-link from config lookup: /panels?panel=ID
  useEffect(() => {
    const raw = searchParams.get("panel");
    if (!raw || !data.length) return;
    if (deepLinkDone.current === raw) return;
    const id = Number(raw);
    if (!Number.isFinite(id)) return;
    const p = data.find((x) => x.id === id);
    if (!p) return;
    deepLinkDone.current = raw;
    setActiveTab(p.category_id == null ? null : p.category_id);
    setSearch(p.name);
    setHighlightId(p.id);
    openEdit(p);
    setSearchParams({}, { replace: true });
    window.setTimeout(() => {
      document
        .getElementById(`panel-card-${p.id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 80);
    // openEdit is stable enough for this mount-time deep link
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, searchParams, setSearchParams]);

  useEffect(() => {
    if (highlightId == null) return;
    const t = window.setTimeout(() => setHighlightId(null), 3500);
    return () => window.clearTimeout(t);
  }, [highlightId]);

  function resetModal() {
    setOpen(false);
    setEditing(null);
    setForm(emptyForm);
    setSslDomain("");
    setSslMsg(null);
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
    setSslDomain("");
    setSslMsg(null);
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
    setSslMsg(null);
    setSslDomain(
      hostFromBaseUrl(
        String((meta.public_host as string | undefined) || p.base_url || "")
      )
    );
    setForm({
      name: p.name,
      type: p.type,
      base_url: p.base_url,
      username: p.username || "",
      password: "",
      api_token: "",
      is_active: p.is_active,
      public_port: String(
        (meta.public_port as number | string | undefined) || "443"
      ),
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
            username: form.username.trim(),
            password: form.password.trim(),
            api_token: form.api_token.trim() || null,
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
    if (isAstra) {
      // WS+TLS behind Cloudflare/nginx (Sanayi-style) — port goes into client links.
      const port = Number(form.public_port) || 443;
      meta.public_port = port;
      meta.inbound_network = "ws";
      meta.tls_terminate = "nginx";
      const host = normalizeBaseUrl(form.base_url)
        .replace(/^https?:\/\//i, "")
        .split("/")[0]
        .split(":")[0];
      if (host) meta.public_host = host;
      return meta;
    }
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
    } else if (isServicePanel) {
      // Rebecca/Guard: configs come from Services — no protocol/inbound meta
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
        // Patch only dirty fields — backend skips remote probe when URL/creds unchanged.
        const body: Record<string, unknown> = {};
        const nextName = form.name.trim();
        const nextUrl = normalizeBaseUrl(form.base_url);
        const nextUser = form.username.trim() || null;
        if (nextName !== editing.name) body.name = nextName;
        if (nextUrl !== normalizeBaseUrl(editing.base_url || "")) body.base_url = nextUrl;
        if (nextUser !== (editing.username || null)) body.username = nextUser;
        if (form.is_active !== editing.is_active) body.is_active = form.is_active;
        if (form.password.trim()) body.password = form.password.trim();
        if (form.api_token.trim()) body.api_token = form.api_token.trim();
        const prevMeta = JSON.stringify(editing.meta || {});
        const nextMeta = JSON.stringify(meta);
        if (prevMeta !== nextMeta) body.meta = meta;
        if (Object.keys(body).length === 0) {
          return editing;
        }
        const needsProbe = Boolean(
          body.base_url || body.username || body.password || body.api_token
        );
        return api<Panel>(`/api/admin/panels/${editing.id}`, {
          method: "PUT",
          body: JSON.stringify(body),
          timeoutMs: needsProbe ? 45_000 : 12_000,
        });
      }
      return api<Panel>("/api/admin/panels", {
        method: "POST",
        body: JSON.stringify({
          name: form.name,
          type: form.type,
          base_url: normalizeBaseUrl(form.base_url),
          username: isAstra ? null : form.username || null,
          password: isAstra ? null : form.password || null,
          api_token: isAstra ? null : isSanaei ? form.api_token || null : form.api_token || null,
          is_active: form.is_active,
          category_id: activeTab,
          meta,
        }),
        timeoutMs: 45_000,
      });
    },
    onSuccess: (p) => {
      qc.setQueryData<Panel[]>(["panels"], (prev) => {
        if (!prev) return [p];
        const idx = prev.findIndex((x) => x.id === p.id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = p;
          return next;
        }
        return [...prev, p];
      });
      void qc.invalidateQueries({ queryKey: ["panels"] });
      void qc.invalidateQueries({ queryKey: ["panel-categories"] });
      resetModal();
      if (!isEdit && p?.type === "astra") {
        setMsg("پنل آسترا ساخته شد — اوتباندها را اضافه کنید");
      } else {
        setMsg(isEdit ? "پنل به‌روزرسانی شد" : "اتصال موفق بود — پنل اضافه شد");
      }
    },
    onError: (e: Error) => setMsg(e.message),
  });

  const reorderMut = useMutation({
    mutationFn: (panel_ids: number[]) =>
      api<Panel[]>("/api/admin/panels/reorder", {
        method: "PUT",
        body: JSON.stringify({
          panel_ids,
          category_id: activeTab,
        }),
        timeoutMs: 15_000,
      }),
    onSuccess: (rows) => {
      qc.setQueryData<Panel[]>(["panels"], (prev) => {
        const byId = new Map(rows.map((p) => [p.id, p]));
        const merged = (prev || []).map((p) => byId.get(p.id) || p);
        // Include any returned rows missing from cache
        for (const p of rows) {
          if (!merged.some((x) => x.id === p.id)) merged.push(p);
        }
        return [...merged].sort((a, b) => {
          const sa = a.sort_order ?? 0;
          const sb = b.sort_order ?? 0;
          if (sa !== sb) return sa - sb;
          return a.id - b.id;
        });
      });
      void qc.invalidateQueries({ queryKey: ["panels"] });
      setReorderMode(false);
      setReorderIds([]);
      setMsg("ترتیب پنل‌ها ذخیره شد");
    },
    onError: (e: Error) => setMsg(e.message || "ذخیره ترتیب ناموفق بود"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/api/admin/panels/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["panels"] });
      qc.invalidateQueries({ queryKey: ["panel-categories"] });
      setConfirmPanel(null);
      setMsg("پنل حذف شد");
    },
    onError: (e: Error) => {
      setConfirmPanel(null);
      setMsg(e.message);
    },
  });

  const moveMut = useMutation({
    mutationFn: ({ id, category_id }: { id: number; category_id: number | null }) =>
      api<Panel>(`/api/admin/panels/${id}/move`, {
        method: "PUT",
        body: JSON.stringify({ category_id }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["panels"] });
      qc.invalidateQueries({ queryKey: ["panel-categories"] });
      setMovePanel(null);
      setMsg("دسته پنل عوض شد");
    },
    onError: (e: Error) => setMsg(e.message),
  });

  const saveCategory = useMutation({
    mutationFn: async () => {
      const n = catName.trim();
      if (!n) throw new Error("نام دسته را وارد کنید");
      if (catModal === "create") {
        return api<PanelCategory>("/api/admin/panel-categories", {
          method: "POST",
          body: JSON.stringify({ name: n }),
        });
      }
      if (catModal && typeof catModal === "object" && "edit" in catModal) {
        return api<PanelCategory>(`/api/admin/panel-categories/${catModal.edit.id}`, {
          method: "PUT",
          body: JSON.stringify({ name: n }),
        });
      }
      throw new Error("invalid");
    },
    onSuccess: (cat) => {
      qc.invalidateQueries({ queryKey: ["panel-categories"] });
      setCatModal(null);
      setCatName("");
      if (catModal === "create") setActiveTab(cat.id);
    },
  });

  const deleteCategory = useMutation({
    mutationFn: (id: number) =>
      api(`/api/admin/panel-categories/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["panel-categories"] });
      qc.invalidateQueries({ queryKey: ["panels"] });
      setActiveTab(null);
      setCatModal(null);
    },
  });

  const [testToast, setTestToast] = useState<{
    ok: boolean;
    text: string;
  } | null>(null);
  const testToastTimer = useRef<number | null>(null);

  function showTestToast(ok: boolean, text: string) {
    if (testToastTimer.current) window.clearTimeout(testToastTimer.current);
    setTestToast({ ok, text });
    testToastTimer.current = window.setTimeout(() => {
      setTestToast(null);
      testToastTimer.current = null;
    }, 3200);
  }

  useEffect(() => {
    return () => {
      if (testToastTimer.current) window.clearTimeout(testToastTimer.current);
    };
  }, []);

  const [testAllOpen, setTestAllOpen] = useState(false);
  const [testAllFilter, setTestAllFilter] = useState<
    "all" | "fail" | "slow" | "ok" | "fast"
  >("all");

  type PanelTestGrade = "fast" | "ok" | "slow" | "fail";
  type PanelTestAll = {
    results: {
      id: number;
      name: string;
      type: string;
      is_active: boolean;
      success: boolean;
      error?: string | null;
      /** Astra hops without ping — show as قطع even when panel is still «وصل». */
      warning?: string | null;
      latency_ms?: number | null;
      grade?: PanelTestGrade | string;
      base_url?: string | null;
      worker_id?: number | null;
      worker_name?: string | null;
      via_worker?: boolean;
    }[];
    ok: number;
    fail: number;
    stats?: {
      total: number;
      ok: number;
      fail: number;
      active_ok: number;
      active_fail: number;
      inactive_ok: number;
      inactive_fail: number;
      fast: number;
      normal: number;
      slow: number;
      avg_latency_ms?: number | null;
      median_latency_ms?: number | null;
      best_latency_ms?: number | null;
      worst_latency_ms?: number | null;
      duration_ms: number;
    };
    tested_at?: string | null;
  };

  type WorkerBrief = { id: number; name: string; status?: string; is_active?: boolean };

  const workersQ = useQuery({
    queryKey: ["workers-brief"],
    queryFn: () => api<WorkerBrief[]>("/api/admin/workers"),
    staleTime: 60_000,
  });
  const workerNameById = useMemo(() => {
    const map = new Map<number, string>();
    for (const w of workersQ.data || []) {
      map.set(Number(w.id), String(w.name || `worker-${w.id}`));
    }
    return map;
  }, [workersQ.data]);

  function workerLabelForPanel(p: Panel): string | null {
    const wid = p.worker_id;
    if (wid == null) return null;
    return workerNameById.get(Number(wid)) || `ورکر ${toFaDigits(wid)}`;
  }

  function formatLatency(ms: number | null | undefined): string {
    if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
    if (ms < 1000) return `${toFaDigits(Math.round(ms))}ms`;
    return `${toFaDigits((Math.round(ms / 10) / 100).toFixed(2))}s`;
  }

  function gradeLabel(grade: string | undefined, success: boolean): string {
    if (!success || grade === "fail") return "قطع";
    if (grade === "fast") return "سریع";
    if (grade === "slow") return "کند";
    return "معمولی";
  }

  function gradeClass(grade: string | undefined, success: boolean): string {
    if (!success || grade === "fail") return "fail";
    if (grade === "fast") return "fast";
    if (grade === "slow") return "slow";
    return "ok";
  }

  function outboundHintFromRaw(raw: Record<string, unknown> | undefined | null): string {
    if (!raw || typeof raw !== "object") return "";
    const warn = typeof raw.outbound_warning === "string" ? raw.outbound_warning.trim() : "";
    if (warn) return ` · ${warn}`;
    const ob = raw.outbounds as
      | {
          bad_names?: string[];
          disabled_names?: string[];
          ok?: number;
          enabled?: number;
          bad?: number;
          disabled?: number;
        }
      | undefined;
    if (!ob || typeof ob !== "object") return "";
    const bits: string[] = [];
    const bad = Array.isArray(ob.bad_names) ? ob.bad_names.filter(Boolean) : [];
    const off = Array.isArray(ob.disabled_names)
      ? ob.disabled_names.filter(Boolean)
      : [];
    if (bad.length) bits.push(`قطع: ${bad.slice(0, 3).map((n) => `«${n}»`).join("، ")}`);
    if (off.length) bits.push(`خاموش: ${off.slice(0, 3).map((n) => `«${n}»`).join("، ")}`);
    return bits.length ? ` · ${bits.join(" · ")}` : "";
  }

  const test = useMutation({
    mutationFn: (id: number) =>
      api<{
        success: boolean;
        error?: string;
        latency_ms?: number;
        worker_id?: number | null;
        worker_name?: string | null;
        via_worker?: boolean;
        raw?: Record<string, unknown>;
      }>(`/api/admin/panels/${id}/test`, {
        method: "POST",
        timeoutMs: 28_000,
      }),
    onSuccess: (res, id) => {
      const panelName = data.find((p) => p.id === id)?.name;
      const ms =
        typeof res.latency_ms === "number" && res.latency_ms >= 0
          ? ` · ${formatLatency(res.latency_ms)}`
          : "";
      const grade =
        res.success && typeof res.latency_ms === "number"
          ? res.latency_ms <= 1000
            ? "سریع"
            : res.latency_ms <= 3000
              ? "معمولی"
              : "کند"
          : "";
      const wName =
        res.worker_name ||
        (res.worker_id != null
          ? workerNameById.get(Number(res.worker_id)) || null
          : null);
      const via =
        wName != null
          ? res.via_worker
            ? ` · ورکر «${wName}»`
            : ` · ورکر «${wName}» (نود مستر)`
          : "";
      const obHint = outboundHintFromRaw(res.raw);
      showTestToast(
        !!res.success,
        res.success
          ? panelName
            ? `اتصال «${panelName}» برقرار است${ms}${grade ? ` · ${grade}` : ""}${via}${obHint}`
            : `اتصال برقرار است${ms}${grade ? ` · ${grade}` : ""}${via}${obHint}`
          : `${res.error || "اتصال به پنل ریموت برقرار نشد"}${via}`
      );
      qc.invalidateQueries({ queryKey: ["panels"] });
    },
    onError: (e: Error) => {
      const msg = e.message || "خطای تست";
      // Never show opaque 500 — probe path must return JSON success/error.
      showTestToast(
        false,
        /500|Internal Server Error/i.test(msg)
          ? "خطای داخلی هنگام تست — صفحه را یک‌بار رفرش کنید و دوباره بزنید"
          : msg
      );
    },
  });

  const activeVisiblePanels = useMemo(
    () => visiblePanels.filter((p) => p.is_active),
    [visiblePanels]
  );

  const testAll = useMutation({
    mutationFn: () => {
      // Only active panels in the current tab/search — never خاموش / other cats.
      const ids = activeVisiblePanels.map((p) => p.id);
      return api<PanelTestAll>("/api/admin/panels/test-all", {
        method: "POST",
        body: JSON.stringify({ panel_ids: ids }),
        timeoutMs: 75_000,
      });
    },
    onSuccess: (res) => {
      setTestAllFilter("all");
      setTestAllOpen(true);
      qc.invalidateQueries({ queryKey: ["panels"] });
      const st = res.stats;
      const avg =
        st?.avg_latency_ms != null
          ? ` · میانگین ${formatLatency(st.avg_latency_ms)}`
          : "";
      const total = res.stats?.total ?? res.results?.length ?? res.ok + res.fail;
      if (res.fail === 0) {
        showTestToast(
          true,
          `همه ${toFaDigits(res.ok)} پنل روشن وصل‌اند${avg}`
        );
      } else {
        showTestToast(
          false,
          `${toFaDigits(res.ok)} وصل · ${toFaDigits(res.fail)} قطع از ${toFaDigits(total)} پنل روشن${avg}`
        );
      }
    },
    onError: (e: Error) => {
      showTestToast(false, e.message || "تست کلی ناموفق بود");
    },
  });

  const testAllFiltered = useMemo(() => {
    const rows = testAll.data?.results || [];
    if (testAllFilter === "all") return rows;
    if (testAllFilter === "fail") return rows.filter((r) => !r.success);
    if (testAllFilter === "fast") return rows.filter((r) => r.grade === "fast");
    if (testAllFilter === "slow") return rows.filter((r) => r.grade === "slow");
    if (testAllFilter === "ok") return rows.filter((r) => r.grade === "ok");
    return rows;
  }, [testAll.data, testAllFilter]);

  const issueNodeSsl = useMutation({
    mutationFn: (domain: string) =>
      api<{ ok: boolean; message: string; domain: string }>("/api/admin/settings/ssl", {
        method: "POST",
        body: JSON.stringify({ target: "node", domain }),
      }),
    onSuccess: (res) => {
      setSslMsg({ type: "ok", text: res.message || "SSL صادر شد" });
      const d = (res.domain || sslDomain).trim();
      if (d && isAstra) {
        setForm((prev) => ({
          ...prev,
          base_url: prev.base_url.trim() ? prev.base_url : d,
        }));
        setSslDomain(d);
      }
    },
    onError: (e: Error) => {
      setSslMsg({
        type: "err",
        text: e instanceof ApiError ? e.message : e.message || "خطای SSL",
      });
    },
  });

  const toggleActive = useMutation({
    mutationFn: ({ id, is_active }: { id: number; is_active: boolean }) =>
      api<Panel>(`/api/admin/panels/${id}/active`, {
        method: "PUT",
        body: JSON.stringify({ is_active }),
      }),
    onMutate: async ({ id, is_active }) => {
      await qc.cancelQueries({ queryKey: ["panels"] });
      const prev = qc.getQueryData<Panel[]>(["panels"]);
      if (prev) {
        qc.setQueryData<Panel[]>(
          ["panels"],
          prev.map((p) => (p.id === id ? { ...p, is_active } : p))
        );
      }
      return { prev };
    },
    onError: (e: Error, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(["panels"], ctx.prev);
      setMsg(e.message || "تغییر وضعیت پنل ناموفق بود");
    },
    onSuccess: (panel) => {
      qc.setQueryData<Panel[]>(["panels"], (cur) =>
        (cur || []).map((p) => (p.id === panel.id ? panel : p))
      );
      qc.invalidateQueries({ queryKey: ["groups"] });
      setMsg(
        panel.is_active
          ? `پنل «${panel.name}» روشن شد — در ساب‌ها و گروه‌ها فعال می‌شود و اکانت‌های ناقص ساخته می‌شوند`
          : `پنل «${panel.name}» خاموش شد — از ساب‌های قدیم و جدید و ساخت روی گروه‌ها خارج شد`
      );
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!isAstra) {
      if (isGuard) {
        const hasKey = !!form.api_token.trim();
        const hasUserPass = !!form.username.trim() && (!!form.password.trim() || isEdit);
        if (!hasKey && !hasUserPass) {
          setMsg("برای گارد یوزر/پسورد ادمین یا API Key را وارد کنید");
          return;
        }
      } else {
        if (!form.username.trim()) {
          setMsg("نام کاربری الزامی است");
          return;
        }
        if (!isEdit && !form.password.trim()) {
          setMsg("رمز عبور الزامی است");
          return;
        }
      }
      if (!isEdit && isSanaei && !form.api_token.trim()) {
        setMsg("برای سنایی API Token الزامی است");
        return;
      }
    } else if (!form.base_url.trim()) {
      setMsg("آدرس/هاست عمومی نود الزامی است");
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
      <p className="page-sub">
        اتصال به آسترا (اوتباند)، سنایی، پاسارگاد، مرزبان و ربکا. با دکمه روشن/خاموش، پنل از{" "}
        <b>همه ساب‌های قدیم و جدید</b> و ساخت روی گروه‌ها خارج یا دوباره فعال می‌شود.
      </p>

      <div className="toolbar">
        <div className="admin-search">
          <Search size={16} strokeWidth={2} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="جستجوی پنل (نام، آدرس، نوع…)"
            aria-label="جستجوی پنل"
            dir="auto"
          />
          {!!search.trim() && (
            <button
              type="button"
              className="admin-search-clear"
              onClick={() => setSearch("")}
              aria-label="پاک کردن جستجو"
            >
              <X size={14} />
            </button>
          )}
        </div>
        <span className="muted">
          {searchQ
            ? `${toFaDigits(visiblePanels.length)} از ${toFaDigits(data.length)} پنل`
            : `${toFaDigits(data.length)} پنل`}
        </span>
        <button
          type="button"
          className="btn ghost"
          disabled={
            testAll.isPending ||
            activeVisiblePanels.length === 0 ||
            reorderMode
          }
          onClick={() => testAll.mutate()}
          title="فقط پنل‌های روشن همین لیست تست می‌شوند"
        >
          <Wifi size={16} />
          {testAll.isPending
            ? `در حال تست ${toFaDigits(activeVisiblePanels.length)} پنل…`
            : `تست اتصال (${toFaDigits(activeVisiblePanels.length)})`}
        </button>
        {reorderMode ? (
          <>
            <button
              type="button"
              className="btn ghost"
              disabled={reorderMut.isPending}
              onClick={cancelReorder}
            >
              انصراف
            </button>
            <button
              type="button"
              className="btn toolbar-primary"
              disabled={reorderMut.isPending || reorderIds.length < 2}
              onClick={() => reorderMut.mutate(reorderIds)}
            >
              <ListOrdered size={16} />
              {reorderMut.isPending ? "در حال ذخیره…" : "ذخیره ترتیب"}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="btn ghost"
              disabled={visiblePanels.length < 2 || !!searchQ}
              title={
                searchQ
                  ? "برای مرتب‌سازی، جستجو را پاک کنید"
                  : "جابه‌جایی پنل‌های این تب با درگ"
              }
              onClick={beginReorder}
            >
              <ListOrdered size={16} />
              ترتیب پنل‌ها
            </button>
            <button className="btn toolbar-primary" onClick={openCreate}>
              <Plus size={16} />
              افزودن پنل
            </button>
          </>
        )}
      </div>

      {reorderMode ? (
        <div className="panels-reorder-banner" role="status">
          <GripVertical size={16} />
          <span>
            ترتیب «{activeCat?.name || "همه پنل‌ها"}» — ردیف را بکشید و رها کنید، بعد ذخیره کنید
          </span>
        </div>
      ) : null}

      <div className="groups-cat-bar" role="tablist" aria-label="دسته‌بندی پنل‌ها">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab == null}
          className={`groups-cat-tab${activeTab == null ? " active" : ""}`}
          onClick={() => setActiveTab(null)}
        >
          همه پنل‌ها
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
            <span className="groups-cat-count">{toFaDigits(c.panels_count)}</span>
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

      {msg && (
        <p
          className={
            /ناموفق|خطا|الزامی|وارد کنید/.test(msg)
              ? "admin-toast err"
              : /موفق|به‌روز|ذخیره|حذف شد|دسته|ساخته|اضافه|عوض/.test(msg)
                ? "admin-toast ok"
                : "admin-toast err"
          }
        >
          {msg}
        </p>
      )}

      {testToast && (
        <div
          className={`panel-float-toast ${testToast.ok ? "ok" : "err"}`}
          role="status"
          aria-live="polite"
        >
          {testToast.ok ? <CheckCircle2 size={18} /> : <XCircle size={18} />}
          <span>{testToast.text}</span>
        </div>
      )}

      {testAllOpen && testAll.data && (
        <div className="panels-test-all">
          <div className="panels-test-all-head">
            <div className="panels-test-all-title">
              <strong>نتیجه تست اتصال این لیست</strong>
              <span className="muted">
                {toFaDigits(testAll.data.stats?.total ?? testAll.data.results.length)} پنل همین صفحه
                {testAll.data.stats?.duration_ms != null
                  ? ` · ${formatLatency(testAll.data.stats.duration_ms)}`
                  : ""}
              </span>
            </div>
            <button
              type="button"
              className="modal-close"
              onClick={() => setTestAllOpen(false)}
              aria-label="بستن"
            >
              ×
            </button>
          </div>

          <div className="panels-test-stats" aria-label="آمار اتصال">
            <div className="panels-test-stat ok">
              <CheckCircle2 size={16} />
              <div>
                <b>{toFaDigits(testAll.data.stats?.ok ?? testAll.data.ok)}</b>
                <span>وصل</span>
              </div>
            </div>
            <div className="panels-test-stat fail">
              <XCircle size={16} />
              <div>
                <b>{toFaDigits(testAll.data.stats?.fail ?? testAll.data.fail)}</b>
                <span>قطع</span>
              </div>
            </div>
            <div className="panels-test-stat fast">
              <Zap size={16} />
              <div>
                <b>{toFaDigits(testAll.data.stats?.fast ?? 0)}</b>
                <span>سریع ≤۱s</span>
              </div>
            </div>
            <div className="panels-test-stat normal">
              <Gauge size={16} />
              <div>
                <b>{toFaDigits(testAll.data.stats?.normal ?? 0)}</b>
                <span>معمولی</span>
              </div>
            </div>
            <div className="panels-test-stat slow">
              <Timer size={16} />
              <div>
                <b>{toFaDigits(testAll.data.stats?.slow ?? 0)}</b>
                <span>کند (+۳s)</span>
              </div>
            </div>
          </div>

          <div className="panels-test-meta">
            <span>
              وصل: {toFaDigits(testAll.data.stats?.ok ?? testAll.data.ok)}
              {(testAll.data.stats?.fail ?? testAll.data.fail) > 0
                ? ` · قطع: ${toFaDigits(testAll.data.stats?.fail ?? testAll.data.fail)}`
                : ""}
            </span>
            {testAll.data.stats?.avg_latency_ms != null ? (
              <span>
                میانگین {formatLatency(testAll.data.stats.avg_latency_ms)}
                {testAll.data.stats.median_latency_ms != null
                  ? ` · میانه ${formatLatency(testAll.data.stats.median_latency_ms)}`
                  : ""}
              </span>
            ) : null}
            {testAll.data.stats?.best_latency_ms != null ? (
              <span>
                بهترین {formatLatency(testAll.data.stats.best_latency_ms)}
                {testAll.data.stats.worst_latency_ms != null
                  ? ` · بدترین ${formatLatency(testAll.data.stats.worst_latency_ms)}`
                  : ""}
              </span>
            ) : null}
          </div>

          <div className="panels-test-filters" role="tablist" aria-label="فیلتر نتیجه">
            {(
              [
                ["all", "همه", testAll.data.stats?.total ?? testAll.data.results.length],
                ["fail", "قطع", testAll.data.stats?.fail ?? testAll.data.fail],
                ["slow", "کند", testAll.data.stats?.slow ?? 0],
                ["ok", "معمولی", testAll.data.stats?.normal ?? 0],
                ["fast", "سریع", testAll.data.stats?.fast ?? 0],
              ] as const
            ).map(([key, label, count]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={testAllFilter === key}
                className={`panels-test-filter${testAllFilter === key ? " active" : ""}`}
                onClick={() => setTestAllFilter(key)}
              >
                {label}
                <em>{toFaDigits(count)}</em>
              </button>
            ))}
          </div>

          <ul className="panels-test-all-list">
            {testAllFiltered.length === 0 ? (
              <li className="empty">
                <span>موردی در این فیلتر نیست</span>
              </li>
            ) : (
              testAllFiltered.map((r) => {
                const typeLabel =
                  TYPES.find((t) => t.value === r.type)?.label || r.type;
                const gClass = gradeClass(r.grade, r.success);
                return (
                  <li key={r.id} className={gClass}>
                    <div className="panels-test-row-main">
                      <span className="panels-test-row-name">
                        {r.success ? (
                          <CheckCircle2 size={15} />
                        ) : (
                          <XCircle size={15} />
                        )}
                        <b>{r.name}</b>
                        <em className="panels-test-type">{typeLabel}</em>
                        {r.worker_name ? (
                          <em className="panels-test-type">
                            {r.via_worker
                              ? `ورکر ${r.worker_name}`
                              : `ورکر ${r.worker_name} · مستر`}
                          </em>
                        ) : null}
                        {!r.is_active ? <em className="panels-test-off">خاموش</em> : null}
                      </span>
                      <span className={`panels-test-grade ${gClass}`}>
                        {gradeLabel(r.grade, r.success)}
                        {r.latency_ms != null ? ` · ${formatLatency(r.latency_ms)}` : ""}
                      </span>
                    </div>
                    <small>
                      {r.success
                        ? [
                            r.warning
                              ? r.warning
                              : r.base_url
                                ? r.base_url
                                : "اتصال برقرار است",
                            r.warning && r.base_url ? r.base_url : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")
                        : r.error || "اتصال ناموفق"}
                    </small>
                  </li>
                );
              })
            )}
          </ul>
        </div>
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
      ) : visiblePanels.length === 0 ? (
        <div className="admin-empty">
          <Server size={36} strokeWidth={1.4} />
          <p>
            {searchQ
              ? "پنلی با این جستجو پیدا نشد"
              : activeTab == null
                ? "پنلی بدون دسته نیست"
                : "در این دسته پنلی نیست"}
          </p>
          {searchQ ? (
            <button className="btn ghost" onClick={() => setSearch("")}>
              پاک کردن جستجو
            </button>
          ) : (
            <button className="btn" onClick={openCreate}>
              افزودن پنل
            </button>
          )}
        </div>
      ) : reorderMode ? (
        <div className="panels-reorder-list" role="list" aria-label="مرتب‌سازی پنل‌ها">
          {reorderPanels.map((p, index) => {
            const typeLabel = TYPES.find((t) => t.value === p.type)?.label || p.type;
            return (
              <div
                key={p.id}
                role="listitem"
                className={`panels-reorder-row${dragOverId === p.id ? " is-over" : ""}${
                  draggingId === p.id ? " is-dragging" : ""
                }`}
                draggable={!reorderMut.isPending}
                onDragStart={(e) => {
                  dragPanelId.current = p.id;
                  setDraggingId(p.id);
                  try {
                    e.dataTransfer.setData("text/plain", String(p.id));
                    e.dataTransfer.effectAllowed = "move";
                  } catch {
                    /* ignore */
                  }
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  try {
                    e.dataTransfer.dropEffect = "move";
                  } catch {
                    /* ignore */
                  }
                  setDragOverId(p.id);
                }}
                onDragLeave={() => {
                  setDragOverId((cur) => (cur === p.id ? null : cur));
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  let from = dragPanelId.current;
                  if (from == null) {
                    try {
                      const raw = e.dataTransfer.getData("text/plain");
                      const n = Number(raw);
                      if (Number.isFinite(n)) from = n;
                    } catch {
                      /* ignore */
                    }
                  }
                  dragPanelId.current = null;
                  setDraggingId(null);
                  setDragOverId(null);
                  if (from != null) moveReorderId(from, p.id);
                }}
                onDragEnd={() => {
                  dragPanelId.current = null;
                  setDraggingId(null);
                  setDragOverId(null);
                }}
              >
                <span className="panels-reorder-handle" aria-hidden>
                  <GripVertical size={18} />
                </span>
                <span className="panels-reorder-index">{toFaDigits(index + 1)}</span>
                <div className="panels-reorder-main">
                  <b>{p.name}</b>
                  <small dir="ltr">{p.base_url}</small>
                </div>
                <div className="panels-reorder-actions">
                  <button
                    type="button"
                    className="btn ghost icon"
                    title="بالا"
                    disabled={reorderMut.isPending || index === 0}
                    onClick={(e) => {
                      e.stopPropagation();
                      shiftReorderId(p.id, -1);
                    }}
                  >
                    <ChevronUp size={16} />
                  </button>
                  <button
                    type="button"
                    className="btn ghost icon"
                    title="پایین"
                    disabled={
                      reorderMut.isPending || index >= reorderPanels.length - 1
                    }
                    onClick={(e) => {
                      e.stopPropagation();
                      shiftReorderId(p.id, 1);
                    }}
                  >
                    <ChevronDown size={16} />
                  </button>
                </div>
                <span className="badge muted">{typeLabel}</span>
                <span className={`badge ${p.is_active ? "ok" : "err"}`}>
                  {p.is_active ? "فعال" : "خاموش"}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="admin-grid">
          {visiblePanels.map((p) => {
            const typeLabel = TYPES.find((t) => t.value === p.type)?.label || p.type;
            const credOk =
              p.type === "astra"
                ? true
                : p.type === "sanaei"
                  ? p.has_token
                  : p.type === "guard"
                    ? p.has_password || p.has_token
                    : p.has_password;
            const credLabel =
              p.type === "astra"
                ? "نود محلی"
                : p.type === "sanaei"
                  ? p.has_token
                    ? "توکن دارد"
                    : "بدون توکن"
                  : p.type === "guard"
                    ? p.has_token
                      ? "API Key"
                      : p.has_password
                        ? "رمز تنظیم شده"
                        : "رمز/کلید ناقص"
                    : p.has_password
                      ? "رمز تنظیم شده"
                      : "رمز ناقص";
            return (
              <article
                key={p.id}
                id={`panel-card-${p.id}`}
                className={`admin-card${p.is_active ? "" : " is-off"}${
                  highlightId === p.id ? " is-highlight" : ""
                }`}
              >
                <div className="admin-card-top">
                  <div className="admin-card-id">
                    <b>{p.name}</b>
                    <div className="admin-card-tags">
                      <span className="badge muted">{typeLabel}</span>
                      <span className={`badge ${p.is_active ? "ok" : "err"}`}>
                        {p.is_active ? "روشن" : "خاموش"}
                      </span>
                      <span className={`badge ${credOk ? "ok" : "err"}`}>{credLabel}</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    className={`panel-power-btn${p.is_active ? " on" : ""}`}
                    disabled={
                      toggleActive.isPending && toggleActive.variables?.id === p.id
                    }
                    title={
                      p.is_active
                        ? "خاموش کردن پنل — از ساب و گروه‌ها خارج می‌شود"
                        : "روشن کردن پنل — به ساب برمی‌گردد و اکانت‌های ناقص ساخته می‌شوند"
                    }
                    aria-pressed={p.is_active}
                    aria-label={p.is_active ? "خاموش کردن پنل" : "روشن کردن پنل"}
                    onClick={() =>
                      toggleActive.mutate({ id: p.id, is_active: !p.is_active })
                    }
                  >
                    <Power size={16} />
                    <span>
                      {toggleActive.isPending && toggleActive.variables?.id === p.id
                        ? "…"
                        : p.is_active
                          ? "روشن"
                          : "خاموش"}
                    </span>
                  </button>
                </div>
                <p className="admin-card-url" dir="ltr" title={p.base_url}>
                  {p.base_url}
                </p>
                {workerLabelForPanel(p) ? (
                  <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.78rem" }}>
                    ورکر: {workerLabelForPanel(p)}
                  </p>
                ) : null}
                <div className="admin-card-actions">
                  {p.type === "astra" && (
                    <Link to={`/panels/${p.id}/outbounds`} className="btn ghost sm">
                      اوتباندها
                    </Link>
                  )}
                  <button type="button" className="btn ghost sm" onClick={() => openEdit(p)}>
                    <Pencil size={14} />
                    ویرایش
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() => setMovePanel(p)}
                    title="جابه‌جایی بین دسته‌ها"
                  >
                    <ArrowRightLeft size={14} />
                    دسته
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() => test.mutate(p.id)}
                    disabled={test.isPending && test.variables === p.id}
                    title={
                      workerLabelForPanel(p)
                        ? `تست از مسیر ورکر «${workerLabelForPanel(p)}»`
                        : "تست اتصال"
                    }
                  >
                    <Wifi size={14} />
                    {test.isPending && test.variables === p.id ? "…" : "تست"}
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
                  کانفیگ‌های این پنل از همه ساب‌ها و از خود پنل اصلی هم حذف می‌شوند، بعد از Astra
                  برداشته می‌شود
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
                  {isAstra
                    ? "پنل محلی اوتباند — بعداً لینک‌ها را در صفحه اوتباندها اضافه کنید"
                    : isEdit
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
                  <label>{isAstra ? "هاست عمومی نود (برای لینک مشتری)" : "آدرس پایه (Base URL)"}</label>
                  <input
                    required
                    placeholder={
                      isAstra
                        ? "panel.example.com یا IP عمومی سرور"
                        : isSanaei
                          ? "https://domain:2053/moj  (با webBasePath)"
                          : "https://panel.example.com:8443  (بدون /dashboard)"
                    }
                    value={form.base_url}
                    onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                    dir="ltr"
                  />
                  {isSanaei && (
                    <p className="field-hint">
                      همان آدرسی که مرورگر باز می‌کند تا قبل از صفحه لاگین، مثل{" "}
                      <code dir="ltr">https://ir.example.com:2053/moj</code>
                    </p>
                  )}
                  {isAstra && (
                    <p className="field-hint">
                      دامنهٔ لبه با پروکسی نارنجی Cloudflare؛ SSL را Full بگذارید. پورت
                      عمومی همان پورتی است که داخل لینک مشتری می‌آید.
                    </p>
                  )}
                </div>
                {isAstra && (
                  <div className="field">
                    <label>پورت عمومی (لینک مشتری)</label>
                    <input
                      required
                      value={form.public_port}
                      onChange={(e) =>
                        setForm({ ...form, public_port: e.target.value.replace(/\D/g, "") })
                      }
                      dir="ltr"
                      placeholder="443"
                      inputMode="numeric"
                    />
                    <p className="field-hint">
                      مثل سنایی: پورت <code dir="ltr">443</code> + ابر نارنجی کلادفلر.
                      پورت‌های دیگر CF:{" "}
                      <code dir="ltr">2053, 2083, 2087, 2096, 8443</code>
                    </p>
                  </div>
                )}
                {isAstra && sslAvailable && (
                  <div className="field">
                    <label>SSL دامنهٔ لبه (Let’s Encrypt)</label>
                    <div className="form-grid" style={{ alignItems: "end" }}>
                      <input
                        dir="ltr"
                        value={sslDomain}
                        onChange={(e) => {
                          setSslDomain(e.target.value);
                          setSslMsg(null);
                        }}
                        onBlur={() => {
                          if (!sslDomain.trim() && form.base_url.trim()) {
                            setSslDomain(hostFromBaseUrl(form.base_url));
                          }
                        }}
                        placeholder="out.example.com"
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={
                          issueNodeSsl.isPending || !sslDomain.trim()
                        }
                        onClick={() => {
                          const d =
                            sslDomain.trim() || hostFromBaseUrl(form.base_url);
                          if (!d) return;
                          setSslDomain(d);
                          setSslMsg(null);
                          issueNodeSsl.mutate(d);
                        }}
                      >
                        <ShieldCheck size={16} />
                        {issueNodeSsl.isPending
                          ? "در حال صدور…"
                          : "دریافت SSL خودکار"}
                      </button>
                    </div>
                    <p className="field-hint">
                      DNS دامنه را به IP همین سرور بزن (برای صدور بهتر موقتاً ابر خاکستری)،
                      بعد SSL بگیر و همان دامنه را به‌عنوان هاست عمومی پنل اوتباند ذخیره کن.
                    </p>
                    {sslMsg && (
                      <p
                        className="field-hint"
                        style={{
                          marginTop: 8,
                          color:
                            sslMsg.type === "ok" ? "var(--ok)" : "var(--danger)",
                        }}
                      >
                        {sslMsg.text}
                      </p>
                    )}
                  </div>
                )}
                {!isAstra && (
                  <div className="form-grid">
                    <div className="field">
                      <label>
                        {isGuard ? "نام کاربری ادمین (یا API Key پایین)" : "نام کاربری ادمین *"}
                      </label>
                      <input
                        required={!isGuard}
                        name="panel_admin_username"
                        autoComplete="off"
                        value={form.username}
                        onChange={(e) => setForm({ ...form, username: e.target.value })}
                        dir="ltr"
                      />
                    </div>
                    <div className="field">
                      <label>
                        {isEdit
                          ? "رمز عبور (خالی = بدون تغییر)"
                          : isGuard
                            ? "رمز عبور"
                            : "رمز عبور *"}
                      </label>
                      <input
                        required={!isEdit && !isGuard}
                        type="password"
                        name="panel_admin_password"
                        value={form.password}
                        onChange={(e) => setForm({ ...form, password: e.target.value })}
                        dir="ltr"
                        autoComplete="new-password"
                      />
                    </div>
                  </div>
                )}

                {(isSanaei || isGuard) && (
                  <div className="field">
                    <label>
                      {isGuard
                        ? isEdit
                          ? "API Key (خالی = بدون تغییر)"
                          : "API Key (اختیاری اگر یوزر/پسورد دارید)"
                        : isEdit
                          ? "API Token (خالی = بدون تغییر)"
                          : "API Token *"}
                    </label>
                    <input
                      required={!isEdit && isSanaei}
                      placeholder={
                        isGuard ? "X-API-Key از پروفایل ادمین گارد" : "Settings → Security → API Token"
                      }
                      value={form.api_token}
                      onChange={(e) => setForm({ ...form, api_token: e.target.value })}
                      dir="ltr"
                    />
                    <p className="field-hint">
                      {isGuard
                        ? "یا یوزر/پسورد بدهید تا Astra خودش JWT بگیرد، یا فقط API Key."
                        : "توکن را کامل کپی کنید. اگر اشتباه باشد سنایی اغلب خطای ۴۰۴ می‌دهد (نه ۴۰۱)."}
                    </p>
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

              {isServicePanel && (
                <section className="panel-section">
                  <div className="inbound-head">
                    <div>
                      <h4>{isGuard ? "گارد" : "ربکا"}</h4>
                      <p className="field-hint">
                        {isGuard
                          ? "در گارد انتخاب پروتکل/اینباند وجود ندارد. با ساخت کاربر، کانفیگ‌ها از سرویس‌های داشبورد گارد می‌آیند. سرویس را در گروه انتخاب کنید (خالی = اولین سرویس)."
                          : "در ربکا انتخاب پروتکل/اینباند وجود ندارد. با ساخت کاربر، کانفیگ‌ها از سرویس‌های تعریف‌شده در داشبورد ربکا می‌آیند. سرویس را هنگام ساخت گروه انتخاب کنید (خالی = اولین سرویس)."}
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
                    ? isEdit
                      ? "در حال ذخیره…"
                      : "در حال تست اتصال…"
                    : isEdit
                      ? "ذخیره تغییرات"
                      : "تست و افزودن"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {movePanel && (
        <div
          className="modal-backdrop"
          onClick={() => !moveMut.isPending && setMovePanel(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>جابه‌جایی پنل</h3>
                <p className="modal-sub">
                  «{movePanel.name}» را به کدام دسته ببرید؟ فقط برای مدیریت در این صفحه
                  است و روی ساب/گروه اثر ندارد.
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setMovePanel(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="groups-move-list">
              <button
                type="button"
                className={`groups-move-opt${movePanel.category_id == null ? " current" : ""}`}
                disabled={moveMut.isPending || movePanel.category_id == null}
                onClick={() =>
                  moveMut.mutate({ id: movePanel.id, category_id: null })
                }
              >
                <span>همه پنل‌ها</span>
                <span className="muted">بدون دسته</span>
              </button>
              {categories.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className={`groups-move-opt${
                    movePanel.category_id === c.id ? " current" : ""
                  }`}
                  disabled={moveMut.isPending || movePanel.category_id === c.id}
                  onClick={() =>
                    moveMut.mutate({ id: movePanel.id, category_id: c.id })
                  }
                >
                  <span>{c.name}</span>
                  <span className="muted">
                    {toFaDigits(c.panels_count)} پنل
                  </span>
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
                onClick={() => setMovePanel(null)}
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
          onClick={() =>
            !saveCategory.isPending && !deleteCategory.isPending && setCatModal(null)
          }
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>{catModal === "create" ? "دسته جدید" : "ویرایش دسته"}</h3>
                <p className="modal-sub">
                  {catModal === "create"
                    ? "مثلاً ایران، خارج، تانل"
                    : "تغییر نام یا حذف دسته — پنل‌هایش به «همه پنل‌ها» برمی‌گردند"}
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
                  placeholder="مثلاً ایران"
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
                          `فقط دسته «${catModal.edit.name}» حذف شود؟\nپنل‌های داخلش پاک نمی‌شوند و به تب «همه پنل‌ها» منتقل می‌شوند.`
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
