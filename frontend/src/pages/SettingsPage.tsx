import { FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Cable,
  Check,
  KeyRound,
  Link2,
  Megaphone,
  Pencil,
  Plus,
  RotateCcw,
  Save,
  Search,
  Server,
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

type SubEdgeStatus = {
  ok: boolean;
  mode: "cloudflare" | "iran_tunnel" | string;
  tunnel_active: boolean;
  tunnel_enabled: boolean;
  watch_enabled: boolean;
  edge_host: string;
  edge: { reachable?: boolean; host?: string; port?: number; latency_ms?: number; error?: string };
  sub_base_url: string;
  sub_hostname: string;
  panel_base_url: string;
  panel_ip_hint: string;
  cf_instructions: string[];
  message?: string;
  notes?: string[];
};

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

type WorkerPanelBrief = {
  id: number;
  name: string;
  type: string;
  is_active: boolean;
};

type WorkerNode = {
  id: number;
  name: string;
  base_url: string;
  is_active: boolean;
  status: string;
  last_seen_at: string | null;
  last_error: string | null;
  ssh_host: string | null;
  ssh_user: string | null;
  listen_port: number;
  panels: WorkerPanelBrief[];
  panel_ids: number[];
  token?: string | null;
  install_command?: string | null;
  install_ok?: boolean | null;
  install_log?: string | null;
  message?: string | null;
};

type WorkerProbeResult = {
  ok: boolean;
  worker_id: number;
  name: string;
  status: string;
  agent_ok: boolean;
  agent_error?: string | null;
  agent_latency_ms?: number | null;
  ssh_tcp_ok?: boolean | null;
  last_error?: string | null;
  message: string;
  worker?: WorkerNode | null;
};

type PanelBrief = { id: number; name: string; type: string; is_active: boolean };

type PanelOwner = { id: number; name: string };

function WorkerPanelPicker({
  panels,
  selected,
  onChange,
  disabled,
  currentWorkerId,
  ownership,
}: {
  panels: PanelBrief[];
  selected: number[];
  onChange: (ids: number[]) => void;
  disabled?: boolean;
  /** When set, panels owned by other workers show as taken (exclusive). */
  currentWorkerId?: number | null;
  ownership?: Record<number, PanelOwner>;
}) {
  const [q, setQ] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const activePanels = useMemo(
    () => panels.filter((p) => p.is_active !== false),
    [panels]
  );
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = [...activePanels].sort((a, b) => {
      const aOn = selectedSet.has(a.id) ? 0 : 1;
      const bOn = selectedSet.has(b.id) ? 0 : 1;
      if (aOn !== bOn) return aOn - bOn;
      const aOther =
        ownership?.[a.id] && ownership[a.id].id !== currentWorkerId ? 1 : 2;
      const bOther =
        ownership?.[b.id] && ownership[b.id].id !== currentWorkerId ? 1 : 2;
      if (!selectedSet.has(a.id) && !selectedSet.has(b.id) && aOther !== bOther) {
        return aOther - bOther;
      }
      return a.name.localeCompare(b.name, "fa");
    });
    if (!needle) return list;
    return list.filter(
      (p) =>
        p.name.toLowerCase().includes(needle) ||
        p.type.toLowerCase().includes(needle) ||
        String(p.id).includes(needle) ||
        (ownership?.[p.id]?.name || "").toLowerCase().includes(needle)
    );
  }, [activePanels, q, selectedSet, ownership, currentWorkerId]);
  const allIds = filtered.map((p) => p.id);
  const allOn =
    allIds.length > 0 && allIds.every((id) => selectedSet.has(id));
  const selectedPanels = activePanels.filter((p) => selectedSet.has(p.id));
  const takenElsewhere = useMemo(() => {
    if (!ownership || !currentWorkerId) return 0;
    return activePanels.filter((p) => {
      const o = ownership[p.id];
      return o && o.id !== currentWorkerId && !selectedSet.has(p.id);
    }).length;
  }, [activePanels, ownership, currentWorkerId, selectedSet]);

  function toggle(id: number) {
    if (selectedSet.has(id)) onChange(selected.filter((x) => x !== id));
    else onChange([...selected, id]);
  }

  return (
    <div className="worker-panel-picker">
      <div className="worker-panel-picker-head">
        <div className="worker-panel-search">
          <Search size={15} aria-hidden />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="جستجوی نام، نوع یا ورکر…"
            disabled={disabled}
            autoComplete="off"
          />
        </div>
        <div className="worker-panel-picker-actions">
          <button
            type="button"
            className="worker-panel-link-btn"
            disabled={disabled || allIds.length === 0}
            onClick={() => {
              if (allOn) {
                onChange(selected.filter((id) => !allIds.includes(id)));
              } else {
                onChange(Array.from(new Set([...selected, ...allIds])));
              }
            }}
          >
            {allOn ? "لغو همهٔ فیلتر" : "انتخاب همهٔ فیلتر"}
          </button>
          {selected.length > 0 && (
            <button
              type="button"
              className="worker-panel-link-btn danger"
              disabled={disabled}
              onClick={() => onChange([])}
            >
              پاک‌کردن انتخاب
            </button>
          )}
          <span className="worker-panel-count">
            {toFaDigits(selected.length)} از {toFaDigits(activePanels.length)}
            {takenElsewhere > 0
              ? ` · ${toFaDigits(takenElsewhere)} روی ورکر دیگر`
              : ""}
          </span>
        </div>
      </div>

      {currentWorkerId != null && (
        <p className="worker-panel-hint muted">
          هر پنل فقط روی یک ورکر است. اگر پنلی روی ورکر دیگر باشد، با انتخاب اینجا
          از آنجا برداشته می‌شود.
        </p>
      )}

      {selectedPanels.length > 0 && (
        <div className="worker-panel-selected" aria-label="پنل‌های انتخاب‌شده">
          {selectedPanels.map((p) => (
            <button
              key={p.id}
              type="button"
              className="worker-panel-tag"
              disabled={disabled}
              onClick={() => toggle(p.id)}
              title="حذف از انتخاب"
            >
              <span>{p.name}</span>
              <span aria-hidden>×</span>
            </button>
          ))}
        </div>
      )}

      <div className="worker-panel-list" role="list">
        {filtered.length === 0 ? (
          <p className="muted worker-panel-empty">پنلی پیدا نشد</p>
        ) : (
          filtered.map((p) => {
            const on = selectedSet.has(p.id);
            const owner = ownership?.[p.id];
            const other =
              owner &&
              currentWorkerId != null &&
              owner.id !== currentWorkerId &&
              !on
                ? owner
                : null;
            const moving =
              owner &&
              currentWorkerId != null &&
              owner.id !== currentWorkerId &&
              on
                ? owner
                : null;
            return (
              <label
                key={p.id}
                role="listitem"
                className={`worker-panel-row${on ? " on" : ""}${
                  other ? " taken" : ""
                }${moving ? " moving" : ""}${disabled ? " is-disabled" : ""}`}
                title={
                  other
                    ? `الان روی «${other.name}» — با انتخاب به این ورکر منتقل می‌شود`
                    : moving
                      ? `با ذخیره از «${moving.name}» برداشته می‌شود`
                      : undefined
                }
              >
                <input
                  type="checkbox"
                  checked={on}
                  disabled={disabled}
                  onChange={() => toggle(p.id)}
                />
                <span className="worker-panel-check" aria-hidden>
                  {on ? <Check size={13} strokeWidth={2.5} /> : null}
                </span>
                <span className="worker-panel-row-body">
                  <b>{p.name}</b>
                  <span className="worker-panel-type" dir="ltr">
                    {p.type}
                  </span>
                </span>
                {other ? (
                  <span className="worker-panel-owner">
                    روی {other.name}
                  </span>
                ) : null}
                {moving ? (
                  <span className="worker-panel-owner moving-badge">
                    انتقال از {moving.name}
                  </span>
                ) : null}
              </label>
            );
          })
        )}
      </div>
    </div>
  );
}

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
  const [workerOpen, setWorkerOpen] = useState(false);
  const [workerName, setWorkerName] = useState("");
  const [workerHost, setWorkerHost] = useState("");
  const [workerSshUser, setWorkerSshUser] = useState("root");
  const [workerSshPass, setWorkerSshPass] = useState("");
  const [workerSshPort, setWorkerSshPort] = useState("22");
  const [workerPort, setWorkerPort] = useState("2090");
  const [workerPanelIds, setWorkerPanelIds] = useState<number[]>([]);
  const [createdWorker, setCreatedWorker] = useState<WorkerNode | null>(null);
  const [confirmWorker, setConfirmWorker] = useState<WorkerNode | null>(null);
  const [assignWorker, setAssignWorker] = useState<WorkerNode | null>(null);
  const [reinstallWorker, setReinstallWorker] = useState<WorkerNode | null>(null);
  const [reinstallPass, setReinstallPass] = useState("");
  const [editWorker, setEditWorker] = useState<WorkerNode | null>(null);
  const [editWorkerName, setEditWorkerName] = useState("");
  const [bulkWorkerOpen, setBulkWorkerOpen] = useState(false);
  const [bulkWorkerText, setBulkWorkerText] = useState("");
  const [workerActionMsg, setWorkerActionMsg] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["site-settings"],
    queryFn: () => api<SiteSettings>("/api/admin/settings"),
  });

  const {
    data: subEdge,
    isLoading: subEdgeLoading,
    refetch: refetchSubEdge,
  } = useQuery({
    queryKey: ["sub-edge"],
    queryFn: () => api<SubEdgeStatus>("/api/admin/settings/sub-edge"),
    refetchInterval: 20000,
  });

  const toggleSubEdge = useMutation({
    mutationFn: (enabled: boolean) =>
      api<SubEdgeStatus>("/api/admin/settings/sub-edge", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      }),
    onSuccess: (res) => {
      qc.setQueryData(["sub-edge"], res);
      setMsg({
        type: res.ok ? "ok" : "err",
        text: res.message || (res.mode === "iran_tunnel" ? "تانل ایران" : "حالت کلادفلر"),
      });
    },
    onError: (e: Error) => {
      const ae = e as ApiError;
      setMsg({
        type: "err",
        text: ae.message || e.message || "خطا در تغییر تانل ساب",
      });
    },
  });

  const { data: apiKeys = [], isLoading: keysLoading } = useQuery({
    queryKey: ["api-keys"],
    queryFn: () => api<ApiKey[]>("/api/admin/api-keys"),
  });

  const { data: workers = [], isLoading: workersLoading } = useQuery({
    queryKey: ["workers"],
    queryFn: () => api<WorkerNode[]>("/api/admin/workers"),
    refetchInterval: 20000,
  });

  const { data: allPanels = [] } = useQuery({
    queryKey: ["panels-brief"],
    queryFn: () => api<PanelBrief[]>("/api/admin/panels"),
  });

  const panelOwnership = useMemo(() => {
    const map: Record<number, PanelOwner> = {};
    for (const w of workers) {
      for (const pid of w.panel_ids || []) {
        map[pid] = { id: w.id, name: w.name };
      }
    }
    return map;
  }, [workers]);

  const createWorker = useMutation({
    mutationFn: () =>
      api<WorkerNode>("/api/admin/workers", {
        method: "POST",
        timeoutMs: 480_000,
        body: JSON.stringify({
          name: workerName.trim(),
          ssh_host: workerHost.trim(),
          ssh_user: workerSshUser.trim() || "root",
          ssh_password: workerSshPass,
          ssh_port: Number(workerSshPort) || 22,
          listen_port: Number(workerPort) || 2090,
          panel_ids: workerPanelIds,
          auto_install: true,
        }),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setCreatedWorker(res);
      setWorkerOpen(false);
      setWorkerName("");
      setWorkerHost("");
      setWorkerSshUser("root");
      setWorkerSshPass("");
      setWorkerSshPort("22");
      setWorkerPort("2090");
      setWorkerPanelIds([]);
    },
  });

  const reinstallMut = useMutation({
    mutationFn: () =>
      api<WorkerNode>(`/api/admin/workers/${reinstallWorker!.id}/reinstall`, {
        method: "POST",
        timeoutMs: 480_000,
        body: JSON.stringify({ ssh_password: reinstallPass }),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setCreatedWorker(res);
      setReinstallWorker(null);
      setReinstallPass("");
    },
  });

  const removeWorker = useMutation({
    mutationFn: (id: number) =>
      api(`/api/admin/workers/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setConfirmWorker(null);
    },
  });

  const rotateWorker = useMutation({
    mutationFn: (id: number) =>
      api<WorkerNode>(`/api/admin/workers/${id}/rotate-token`, {
        method: "POST",
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setCreatedWorker(res);
    },
  });

  const probeWorker = useMutation({
    mutationFn: (id: number) =>
      api<WorkerProbeResult>(`/api/admin/workers/${id}/probe`, {
        method: "POST",
        timeoutMs: 30_000,
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setWorkerActionMsg({
        type: res.ok ? "ok" : "err",
        text: res.message || (res.ok ? "آنلاین" : "قطع"),
      });
    },
    onError: (e) => {
      setWorkerActionMsg({
        type: "err",
        text: e instanceof Error ? e.message : "تست ناموفق",
      });
    },
  });

  const renameWorker = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      api<WorkerNode>(`/api/admin/workers/${id}`, {
        method: "PUT",
        body: JSON.stringify({ name }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setEditWorker(null);
      setEditWorkerName("");
      setWorkerActionMsg({ type: "ok", text: "نام ورکر ذخیره شد" });
    },
    onError: (e) => {
      setWorkerActionMsg({
        type: "err",
        text: e instanceof Error ? e.message : "تغییر نام ناموفق",
      });
    },
  });

  const bulkCreateWorkers = useMutation({
    mutationFn: (workers: {
      name: string;
      ssh_host: string;
      ssh_user: string;
      ssh_password: string;
      ssh_port?: number;
      listen_port?: number;
    }[]) =>
      api<{
        created: WorkerNode[];
        failed: { name: string; ssh_host: string; message: string }[];
        message: string;
      }>("/api/admin/workers/bulk", {
        method: "POST",
        timeoutMs: 120_000,
        body: JSON.stringify({
          workers,
          auto_install: true,
          balance_panels: true,
        }),
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setBulkWorkerOpen(false);
      setBulkWorkerText("");
      const failN = res.failed?.length || 0;
      setWorkerActionMsg({
        type: failN ? "err" : "ok",
        text:
          res.message ||
          `${toFaDigits(res.created?.length || 0)} ورکر · ${toFaDigits(failN)} خطا`,
      });
      if (res.created?.[0]) setCreatedWorker(res.created[0]);
    },
    onError: (e) => {
      setWorkerActionMsg({
        type: "err",
        text: e instanceof Error ? e.message : "ساخت گروهی ناموفق",
      });
    },
  });

  const balanceWorkers = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; balanced: Record<string, number[]> }>(
        "/api/admin/workers/balance-panels",
        { method: "POST", timeoutMs: 60_000 }
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      const n = Object.values(res.balanced || {}).reduce(
        (a, ids) => a + (ids?.length || 0),
        0
      );
      setWorkerActionMsg({
        type: "ok",
        text: `پنل‌ها تقسیم شد — ${toFaDigits(n)} پنل روی ${toFaDigits(
          Object.keys(res.balanced || {}).length
        )} ورکر`,
      });
    },
    onError: (e) => {
      setWorkerActionMsg({
        type: "err",
        text: e instanceof Error ? e.message : "تقسیم پنل ناموفق",
      });
    },
  });

  const saveAssign = useMutation({
    mutationFn: ({ id, panel_ids }: { id: number; panel_ids: number[] }) =>
      api<WorkerNode>(`/api/admin/workers/${id}/panels`, {
        method: "PUT",
        body: JSON.stringify({ panel_ids }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workers"] });
      setAssignWorker(null);
    },
  });

  function parseBulkWorkerLines(raw: string) {
    const out: {
      name: string;
      ssh_host: string;
      ssh_user: string;
      ssh_password: string;
    }[] = [];
    for (const line of raw.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith("#")) continue;
      const parts = t.split(/[|\t,]+/).map((x) => x.trim()).filter(Boolean);
      // name|ip|user|pass  OR  name|ip|pass  OR  ip|pass
      if (parts.length >= 4) {
        out.push({
          name: parts[0],
          ssh_host: parts[1],
          ssh_user: parts[2] || "root",
          ssh_password: parts[3],
        });
      } else if (parts.length === 3) {
        out.push({
          name: parts[0],
          ssh_host: parts[1],
          ssh_user: "root",
          ssh_password: parts[2],
        });
      } else if (parts.length === 2) {
        out.push({
          name: `Worker-${parts[0]}`,
          ssh_host: parts[0],
          ssh_user: "root",
          ssh_password: parts[1],
        });
      }
    }
    return out;
  }

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


          <div className="settings-card settings-card-wide">
            <header className="settings-card-head">
              <Cable size={20} />
              <div>
                <h2>تونل ساب ایران</h2>
                <p>
                  پنل همیشه روی سرور خارج سرو می‌دهد. تانل فقط مسیر ایران را باز
                  می‌کند. روشن: اول تانل، بعد DNS به IP ایران (DNS only). خاموش:
                  اول DNS به IP خارج، بعد تانل.
                </p>
              </div>
            </header>

            {subEdgeLoading && !subEdge ? (
              <p className="field-hint">در حال خواندن وضعیت تانل…</p>
            ) : (
              <div className="settings-stack" style={{ gap: 12 }}>
                <div className="field-hint" dir="rtl">
                  حالت:{" "}
                  <strong>
                    {subEdge?.mode === "iran_tunnel"
                      ? "تانل ایران"
                      : "کلادفلر / بدون تانل"}
                  </strong>
                  {" · "}
                  سرویس تانل:{" "}
                  <strong dir="ltr">
                    {subEdge?.tunnel_active ? "active" : "off"}
                  </strong>
                  {" · "}
                  edge{" "}
                  <code dir="ltr">{subEdge?.edge_host || "—"}</code>:{" "}
                  <strong>
                    {subEdge?.edge?.reachable ? "TCP OK" : "unreachable"}
                  </strong>
                  {subEdge?.panel_ip_hint ? (
                    <>
                      {" · "}
                      پنل{" "}
                      <code dir="ltr">{subEdge.panel_ip_hint}</code>
                    </>
                  ) : null}
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  <button
                    type="button"
                    className="btn ghost sm"
                    disabled={
                      toggleSubEdge.isPending ||
                      (!subEdge?.tunnel_active &&
                        subEdge?.mode === "cloudflare")
                    }
                    onClick={() => {
                      setMsg(null);
                      toggleSubEdge.mutate(false);
                    }}
                  >
                    خاموش کردن تانل
                  </button>
                  <button
                    type="button"
                    className="btn toolbar-primary sm"
                    disabled={
                      toggleSubEdge.isPending ||
                      (subEdge?.mode === "iran_tunnel" &&
                        !!subEdge?.tunnel_active)
                    }
                    onClick={() => {
                      setMsg(null);
                      toggleSubEdge.mutate(true);
                    }}
                  >
                    {subEdge?.mode === "iran_tunnel" && !subEdge?.tunnel_active
                      ? "تلاش مجدد روشن کردن"
                      : "روشن کردن تانل ایران"}
                  </button>
                  <button
                    type="button"
                    className="btn ghost sm"
                    disabled={toggleSubEdge.isPending}
                    onClick={() => refetchSubEdge()}
                  >
                    بروزرسانی وضعیت
                  </button>
                </div>

                {!!subEdge?.cf_instructions?.length && (
                  <ol className="field-hint" style={{ margin: 0, paddingInlineStart: 18 }}>
                    {subEdge.cf_instructions.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ol>
                )}
                {!!subEdge?.notes?.length && (
                  <p className="field-hint" dir="ltr">
                    {subEdge.notes.join(" | ")}
                  </p>
                )}
              </div>
            )}
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
              <Server size={20} />
              <div>
                <h2>سرورهای Worker</h2>
                <p>
                  IP و یوزر/رمز SSH بده؛ نصب خودکار می‌شود — ساخت و مصرف پنل‌ها روی
                  Worker، با failover به همین سرور
                </p>
              </div>
            </header>

            {createdWorker && (
              <div
                className={`admin-toast ${createdWorker.install_ok === false ? "err" : "ok"} admin-key-reveal`}
              >
                <div>
                  <strong>
                    {createdWorker.message ||
                      (createdWorker.install_ok
                        ? "Worker نصب و آماده است"
                        : "وضعیت Worker")}
                  </strong>
                  <p className="muted" style={{ marginTop: 6 }} dir="ltr">
                    {createdWorker.base_url} · {createdWorker.status}
                  </p>
                  {createdWorker.install_log ? (
                    <code
                      dir="ltr"
                      className="admin-key-value"
                      style={{
                        whiteSpace: "pre-wrap",
                        maxHeight: 160,
                        overflow: "auto",
                        display: "block",
                        marginTop: 8,
                        fontSize: 11,
                      }}
                    >
                      {createdWorker.install_log.slice(-2500)}
                    </code>
                  ) : null}
                </div>
                <button
                  type="button"
                  className="btn ghost sm"
                  onClick={() => setCreatedWorker(null)}
                >
                  بستن
                </button>
              </div>
            )}

            <div className="toolbar" style={{ margin: "0 0 12px" }}>
              <span className="muted">{toFaDigits(workers.length)} سرور</span>
              <button
                type="button"
                className="btn ghost sm"
                disabled={balanceWorkers.isPending || workers.length < 2}
                onClick={() => balanceWorkers.mutate()}
                title="تقسیم یکنواخت پنل‌ها بین ورکرها"
              >
                {balanceWorkers.isPending ? "…" : "تقسیم پنل‌ها"}
              </button>
              <button
                type="button"
                className="btn ghost sm"
                onClick={() => setBulkWorkerOpen(true)}
              >
                چندتایی
              </button>
              <button
                type="button"
                className="btn toolbar-primary"
                onClick={() => setWorkerOpen(true)}
              >
                <Plus size={16} />
                افزودن سرور
              </button>
            </div>

            {workerActionMsg ? (
              <p
                className={workerActionMsg.type === "ok" ? "ok" : "error"}
                style={{ margin: "0 0 12px" }}
              >
                {workerActionMsg.text}
              </p>
            ) : null}

            {workersLoading ? (
              <p className="muted">در حال بارگذاری…</p>
            ) : workers.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>
                هنوز Workerای اضافه نشده — یک VPS جدا بسازید تا ساخت و مصرف سنگین از
                پنل اصلی جدا شود
              </p>
            ) : (
              <div className="groups-list">
                {workers.map((w) => (
                  <article key={w.id} className="groups-row">
                    <div className="groups-row-main">
                      <div className="groups-row-title">
                        <b>{w.name}</b>
                        <span className="groups-row-desc" dir="ltr">
                          {w.base_url}
                          {w.ssh_host ? ` · ssh ${w.ssh_user || "root"}@${w.ssh_host}` : ""}
                        </span>
                      </div>
                      <div className="groups-row-meta">
                        <span
                          className={`badge ${
                            w.status === "online"
                              ? "ok"
                              : w.status === "offline"
                                ? "err"
                                : "muted"
                          }`}
                        >
                          {w.status === "online"
                            ? "آنلاین"
                            : w.status === "offline"
                              ? "قطع"
                              : "نامشخص"}
                        </span>
                        <span className="badge muted">
                          {toFaDigits(w.panel_ids?.length || 0)} پنل
                        </span>
                        {!w.is_active ? (
                          <span className="badge err">غیرفعال</span>
                        ) : null}
                      </div>
                      {w.last_error ? (
                        <p
                          className="muted"
                          style={{ margin: "6px 0 0", fontSize: 12, lineHeight: 1.45 }}
                          dir="auto"
                          title={w.last_error}
                        >
                          {w.last_error}
                        </p>
                      ) : null}
                    </div>
                    <div className="groups-row-actions">
                      <button
                        type="button"
                        className="btn ghost sm"
                        disabled={probeWorker.isPending}
                        onClick={() => probeWorker.mutate(w.id)}
                        title="تست دقیق agent و پورت SSH"
                      >
                        {probeWorker.isPending ? "…" : "تست"}
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => {
                          setEditWorker(w);
                          setEditWorkerName(w.name);
                        }}
                        title="ویرایش نام"
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => {
                          setAssignWorker(w);
                          setWorkerPanelIds(w.panel_ids || []);
                        }}
                      >
                        پنل‌ها
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => {
                          setReinstallWorker(w);
                          setReinstallPass("");
                        }}
                      >
                        نصب مجدد
                      </button>
                      <button
                        type="button"
                        className="btn ghost sm"
                        onClick={() => rotateWorker.mutate(w.id)}
                      >
                        توکن جدید
                      </button>
                      <button
                        type="button"
                        className="btn danger sm"
                        onClick={() => setConfirmWorker(w)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>

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

      {workerOpen && (
        <div
          className="modal-backdrop"
          onClick={() => !createWorker.isPending && setWorkerOpen(false)}
        >
          <div
            className="modal worker-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-head worker-modal-head">
              <div className="worker-modal-title">
                <span className="worker-modal-icon" aria-hidden>
                  <Server size={20} />
                </span>
                <div>
                  <h3>افزودن سرور Worker</h3>
                  <p className="modal-sub">
                    فقط IP و یوزر/رمز SSH بده — نصب خودکار روی VPS انجام می‌شود
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => !createWorker.isPending && setWorkerOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="worker-modal-banner">
              <Sparkles size={16} aria-hidden />
              <p>
                ساخت و مصرف پنل‌ها روی این سرور اجرا می‌شود؛ اگر قطع شد، خودکار به
                پنل اصلی برمی‌گردد.
              </p>
            </div>
            <form
              className="worker-modal-form"
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                createWorker.mutate();
              }}
            >
              <div className="worker-modal-grid">
                <div className="field">
                  <label>نام نمایشی</label>
                  <input
                    required
                    minLength={2}
                    value={workerName}
                    onChange={(e) => setWorkerName(e.target.value)}
                    placeholder="مثلاً Worker اروپا"
                    disabled={createWorker.isPending}
                  />
                </div>
                <div className="field">
                  <label>IP سرور</label>
                  <input
                    required
                    dir="ltr"
                    value={workerHost}
                    onChange={(e) => setWorkerHost(e.target.value)}
                    placeholder="1.2.3.4"
                    disabled={createWorker.isPending}
                    autoComplete="off"
                  />
                </div>
                <div className="field">
                  <label>یوزر SSH</label>
                  <input
                    required
                    dir="ltr"
                    value={workerSshUser}
                    onChange={(e) => setWorkerSshUser(e.target.value)}
                    placeholder="root"
                    disabled={createWorker.isPending}
                    autoComplete="username"
                  />
                </div>
                <div className="field">
                  <label>رمز SSH</label>
                  <input
                    required
                    type="password"
                    dir="ltr"
                    value={workerSshPass}
                    onChange={(e) => setWorkerSshPass(e.target.value)}
                    disabled={createWorker.isPending}
                    autoComplete="new-password"
                  />
                  <p className="field-hint">ذخیره نمی‌شود — فقط برای نصب یک‌بار</p>
                </div>
                <div className="field">
                  <label>پورت SSH</label>
                  <input
                    dir="ltr"
                    value={workerSshPort}
                    onChange={(e) => setWorkerSshPort(e.target.value)}
                    disabled={createWorker.isPending}
                  />
                </div>
                <div className="field">
                  <label>پورت Worker</label>
                  <input
                    dir="ltr"
                    value={workerPort}
                    onChange={(e) => setWorkerPort(e.target.value)}
                    disabled={createWorker.isPending}
                  />
                </div>
              </div>
              <div className="field worker-modal-panels">
                <label>پنل‌هایی که به این سرور بروند</label>
                <p className="field-hint" style={{ marginTop: 0 }}>
                  تیک بزنید تا درخواست‌های همان پنل‌ها از این Worker برود — بعداً هم قابل تغییر است
                </p>
                <WorkerPanelPicker
                  panels={allPanels}
                  selected={workerPanelIds}
                  onChange={setWorkerPanelIds}
                  disabled={createWorker.isPending}
                  ownership={panelOwnership}
                />
              </div>
              {createWorker.isPending && (
                <p className="worker-modal-pending">
                  در حال نصب خودکار روی سرور… معمولاً ۲ تا ۵ دقیقه طول می‌کشد
                </p>
              )}
              {createWorker.isError && (
                <p className="error">{(createWorker.error as Error).message}</p>
              )}
              <footer className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={createWorker.isPending}
                  onClick={() => setWorkerOpen(false)}
                >
                  انصراف
                </button>
                <button
                  className="btn"
                  disabled={
                    createWorker.isPending ||
                    workerName.trim().length < 2 ||
                    !workerHost.trim() ||
                    !workerSshPass
                  }
                >
                  {createWorker.isPending ? "در حال نصب…" : "ساخت و نصب خودکار"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {reinstallWorker && (
        <div
          className="modal-backdrop"
          onClick={() => !reinstallMut.isPending && setReinstallWorker(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>نصب مجدد — {reinstallWorker.name}</h3>
                <p className="modal-sub" dir="ltr">
                  {reinstallWorker.ssh_user || "root"}@{reinstallWorker.ssh_host}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setReinstallWorker(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="field">
              <label>رمز SSH</label>
              <input
                type="password"
                dir="ltr"
                value={reinstallPass}
                onChange={(e) => setReinstallPass(e.target.value)}
                disabled={reinstallMut.isPending}
                autoComplete="new-password"
              />
            </div>
            {reinstallMut.isPending && (
              <p className="muted">در حال نصب مجدد…</p>
            )}
            {reinstallMut.isError && (
              <p className="error">{(reinstallMut.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={reinstallMut.isPending}
                onClick={() => setReinstallWorker(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn"
                disabled={reinstallMut.isPending || !reinstallPass}
                onClick={() => reinstallMut.mutate()}
              >
                {reinstallMut.isPending ? "…" : "نصب مجدد"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {editWorker && (
        <div
          className="modal-backdrop"
          onClick={() => !renameWorker.isPending && setEditWorker(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>ویرایش نام ورکر</h3>
                <p className="modal-sub" dir="ltr">
                  {editWorker.ssh_host || editWorker.base_url}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setEditWorker(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="field">
              <label>نام نمایشی</label>
              <input
                required
                minLength={2}
                value={editWorkerName}
                onChange={(e) => setEditWorkerName(e.target.value)}
                disabled={renameWorker.isPending}
              />
            </div>
            {renameWorker.isError && (
              <p className="error">{(renameWorker.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={renameWorker.isPending}
                onClick={() => setEditWorker(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn"
                disabled={
                  renameWorker.isPending || editWorkerName.trim().length < 2
                }
                onClick={() =>
                  renameWorker.mutate({
                    id: editWorker.id,
                    name: editWorkerName.trim(),
                  })
                }
              >
                {renameWorker.isPending ? "…" : "ذخیره"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {bulkWorkerOpen && (
        <div
          className="modal-backdrop"
          onClick={() =>
            !bulkCreateWorkers.isPending && setBulkWorkerOpen(false)
          }
        >
          <div
            className="modal worker-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-head worker-modal-head">
              <div className="worker-modal-title">
                <span className="worker-modal-icon" aria-hidden>
                  <Server size={20} />
                </span>
                <div>
                  <h3>افزودن چند Worker</h3>
                  <p className="modal-sub">
                    هر خط یک سرور — بعد از نصب، پنل‌ها خودکار تقسیم می‌شوند
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setBulkWorkerOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="field">
              <label>لیست سرورها</label>
              <textarea
                dir="ltr"
                rows={8}
                value={bulkWorkerText}
                onChange={(e) => setBulkWorkerText(e.target.value)}
                disabled={bulkCreateWorkers.isPending}
                placeholder={
                  "DE-Hetzner-P4|78.18.14.169|root|password\n" +
                  "FI-Hetzner-P4T|5.222.24.95|root|password\n" +
                  "FI-HostVDS|185.179.153.78|password"
                }
                style={{ fontFamily: "ui-monospace, monospace", width: "100%" }}
              />
              <p className="field-hint">
                فرمت: <code>نام|IP|یوزر|رمز</code> یا{" "}
                <code>نام|IP|رمز</code> یا <code>IP|رمز</code>
              </p>
            </div>
            {bulkCreateWorkers.isPending && (
              <p className="worker-modal-pending">
                در حال ساخت و نصب… نصب هر سرور ممکن است چند دقیقه طول بکشد
              </p>
            )}
            {bulkCreateWorkers.isError && (
              <p className="error">
                {(bulkCreateWorkers.error as Error).message}
              </p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={bulkCreateWorkers.isPending}
                onClick={() => setBulkWorkerOpen(false)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn"
                disabled={
                  bulkCreateWorkers.isPending ||
                  parseBulkWorkerLines(bulkWorkerText).length < 1
                }
                onClick={() =>
                  bulkCreateWorkers.mutate(parseBulkWorkerLines(bulkWorkerText))
                }
              >
                {bulkCreateWorkers.isPending
                  ? "در حال نصب…"
                  : `ساخت ${toFaDigits(
                      parseBulkWorkerLines(bulkWorkerText).length
                    )} ورکر`}
              </button>
            </footer>
          </div>
        </div>
      )}

      {assignWorker && (
        <div
          className="modal-backdrop"
          onClick={() => !saveAssign.isPending && setAssignWorker(null)}
        >
          <div
            className="modal worker-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-head worker-modal-head">
              <div className="worker-modal-title">
                <span className="worker-modal-icon" aria-hidden>
                  <Server size={20} />
                </span>
                <div>
                  <h3>تخصیص پنل — {assignWorker.name}</h3>
                  <p className="modal-sub">
                    تخصیص انحصاری — پنل انتخاب‌شده از ورکر دیگر برداشته می‌شود
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setAssignWorker(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <div className="field worker-modal-panels" style={{ marginTop: 4 }}>
              <WorkerPanelPicker
                panels={allPanels}
                selected={workerPanelIds}
                onChange={setWorkerPanelIds}
                disabled={saveAssign.isPending}
                currentWorkerId={assignWorker.id}
                ownership={panelOwnership}
              />
            </div>
            {saveAssign.isError && (
              <p className="error">{(saveAssign.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={saveAssign.isPending}
                onClick={() => setAssignWorker(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn"
                disabled={saveAssign.isPending}
                onClick={() =>
                  saveAssign.mutate({
                    id: assignWorker.id,
                    panel_ids: workerPanelIds,
                  })
                }
              >
                {saveAssign.isPending ? "…" : "ذخیره تخصیص"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {confirmWorker && (
        <div
          className="modal-backdrop"
          onClick={() => !removeWorker.isPending && setConfirmWorker(null)}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>حذف Worker</h3>
                <p className="modal-sub">
                  «{confirmWorker.name}» حذف می‌شود و پنل‌هایش به سرور اصلی برمی‌گردند
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmWorker(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            {removeWorker.isError && (
              <p className="error">{(removeWorker.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={removeWorker.isPending}
                onClick={() => setConfirmWorker(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={removeWorker.isPending}
                onClick={() => removeWorker.mutate(confirmWorker.id)}
              >
                {removeWorker.isPending ? "…" : "حذف"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
