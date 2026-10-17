import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Cable,
  Eye,
  EyeOff,
  Layers,
  Loader2,
  RefreshCw,
  Search,
  Server,
  Sparkles,
  Trash2,
} from "lucide-react";
import { ApiError, Group, api } from "../lib/api";
import { toFaDigits } from "../lib/format";

type VisibilityItem = {
  host: string;
  port: number;
  match_host?: string;
  label: string;
  protocol: string;
  count: number;
  panel_names: string[];
  hidden: boolean;
  orphan: boolean;
  group_id?: number | null;
  group_name?: string | null;
};

type VisibilityOut = {
  group_id: number;
  group_name: string;
  sample_ready: boolean;
  sample_username?: string | null;
  items: VisibilityItem[];
  hidden_count: number;
  total_count: number;
};

type VisibilityAllOut = {
  items: VisibilityItem[];
  hidden_count: number;
  total_count: number;
  groups_count: number;
  sample_ready_groups: number;
};

type SampleWorkspace = {
  sample_username?: string | null;
  sample_ready?: boolean;
  accounts?: {
    panel_name?: string | null;
    status: string;
    links_count: number;
    error_message?: string | null;
  }[];
  items?: unknown[];
};

type SampleAllOut = {
  ok_count: number;
  fail_count: number;
  groups: {
    group_id: number;
    group_name: string;
    ok: boolean;
    sample_username?: string | null;
    items_count: number;
    error?: string | null;
  }[];
  visibility?: VisibilityAllOut | null;
};

type Scope = "group" | "all";

function rowKey(row: VisibilityItem, scope: Scope): string {
  const mh = (row.match_host || "").trim();
  const ep = mh ? `${row.host}:${row.port}|${mh}` : `${row.host}:${row.port}`;
  if (scope === "all" && row.group_id != null) return `${row.group_id}::${ep}`;
  return ep;
}

function endpointLabel(row: VisibilityItem): string {
  const base = `${row.host}:${row.port}`;
  const mh = (row.match_host || "").trim();
  return mh ? `${base} · ${mh}` : base;
}

function protoBadge(protocol: string): string {
  const p = (protocol || "").toLowerCase();
  if (!p) return "CFG";
  if (p.startsWith("vless")) return "VLESS";
  if (p.startsWith("vmess")) return "VMESS";
  if (p.startsWith("trojan")) return "TROJAN";
  if (p === "ss" || p.startsWith("shadow")) return "SS";
  if (p.startsWith("hysteria")) return "HY2";
  return p.slice(0, 5).toUpperCase();
}

export default function ConfigsPage() {
  const qc = useQueryClient();
  const [scope, setScope] = useState<Scope>("group");
  const [groupId, setGroupId] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "visible" | "hidden">("all");
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const { data: groups = [], isLoading: groupsLoading } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<Group[]>("/api/groups"),
  });

  const activeGroups = useMemo(
    () =>
      groups
        .filter((g) => !g.is_disabled)
        .sort((a, b) => a.name.localeCompare(b.name, "fa")),
    [groups]
  );

  useEffect(() => {
    if (groupId == null && activeGroups.length) {
      setGroupId(activeGroups[0].id);
    }
  }, [activeGroups, groupId]);

  const selectedId = groupId;
  const selectedGroup = activeGroups.find((g) => g.id === selectedId) || null;
  const isAll = scope === "all";

  const groupVis = useQuery({
    queryKey: ["config-visibility", selectedId],
    queryFn: () =>
      api<VisibilityOut>(`/api/group/${selectedId}/config-visibility`),
    enabled: !isAll && selectedId != null,
  });

  const allVis = useQuery({
    queryKey: ["config-visibility-all"],
    queryFn: () => api<VisibilityAllOut>("/api/admin/config-visibility"),
    enabled: isAll,
  });

  const visibilityItems = isAll
    ? allVis.data?.items || []
    : groupVis.data?.items || [];
  const totalCount = isAll
    ? allVis.data?.total_count || 0
    : groupVis.data?.total_count || 0;
  const hiddenCount = isAll
    ? allVis.data?.hidden_count || 0
    : groupVis.data?.hidden_count || 0;
  const sampleReady = isAll
    ? (allVis.data?.sample_ready_groups || 0) > 0
    : !!groupVis.data?.sample_ready;
  const visLoading = isAll ? allVis.isLoading : groupVis.isLoading;
  const visFetching = isAll ? allVis.isFetching : groupVis.isFetching;
  const visError = isAll ? allVis.error : groupVis.error;

  /** Light: only reload DB aggregate (no remote provision). */
  const reloadAll = useMutation({
    mutationFn: () => api<VisibilityAllOut>("/api/admin/config-visibility"),
    onSuccess: (vis) => {
      qc.setQueryData(["config-visibility-all"], vis);
      setMsg({
        type: "ok",
        text: `${toFaDigits(vis.total_count)} کانفیگ از همه پنل‌ها (از دیتابیس)`,
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در بارگذاری",
      });
    },
  });

  /** Heavy: create/refresh probe on every active group, then refresh all-visibility. */
  const refreshAllSamples = useMutation({
    mutationFn: () =>
      api<SampleAllOut>("/api/admin/configs/sample", { method: "POST" }),
    onSuccess: (res) => {
      if (res.visibility) {
        qc.setQueryData(["config-visibility-all"], res.visibility);
      } else {
        qc.invalidateQueries({ queryKey: ["config-visibility-all"] });
      }
      qc.invalidateQueries({ queryKey: ["config-visibility"] });
      const n = res.visibility?.total_count ?? 0;
      if (res.fail_count > 0) {
        setMsg({
          type: "err",
          text: `ساخت روی ${toFaDigits(res.ok_count)} گروه OK · ${toFaDigits(res.fail_count)} گروه خطا · ${toFaDigits(n)} کانفیگ`,
        });
      } else {
        setMsg({
          type: "ok",
          text: `نمونه روی ${toFaDigits(res.ok_count)} گروه ساخته شد · ${toFaDigits(n)} کانفیگ یکتا`,
        });
      }
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در ساخت همه نمونه‌ها",
      });
    },
  });

  /** Remove probe users from every active group; keep hide rules. */
  const deleteAllSamples = useMutation({
    mutationFn: () =>
      api<SampleAllOut>("/api/admin/configs/sample", { method: "DELETE" }),
    onSuccess: (res) => {
      if (res.visibility) {
        qc.setQueryData(["config-visibility-all"], res.visibility);
      } else {
        qc.invalidateQueries({ queryKey: ["config-visibility-all"] });
      }
      qc.invalidateQueries({ queryKey: ["config-visibility"] });
      setMsg({
        type: res.fail_count > 0 ? "err" : "ok",
        text:
          res.fail_count > 0
            ? `حذف از ${toFaDigits(res.ok_count)} گروه OK · ${toFaDigits(res.fail_count)} خطا · مخفی‌ها حفظ شد`
            : `کاربرهای تستی از ${toFaDigits(res.ok_count)} گروه حذف شدند · مخفی‌ها حفظ شد`,
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در حذف نمونه‌ها",
      });
    },
  });

  /** Heavy only for ONE selected group — creates test user on that group's panels. */
  const refreshSample = useMutation({
    mutationFn: async () => {
      if (selectedId == null) throw new Error("گروه انتخاب نشده");
      const ws = await api<SampleWorkspace>(
        `/api/group/${selectedId}/configs/sample`,
        { method: "POST" }
      );
      const vis = await api<VisibilityOut>(
        `/api/group/${selectedId}/config-visibility`
      );
      return { ws, vis };
    },
    onSuccess: ({ ws, vis }) => {
      qc.setQueryData(["config-visibility", selectedId], vis);
      qc.invalidateQueries({ queryKey: ["config-visibility-all"] });
      const n = vis.total_count;
      const errPanels = (ws.accounts || []).filter(
        (a) => a.status === "error" || (a.error_message || "").trim()
      ).length;
      if (n > 0) {
        setMsg({
          type: "ok",
          text: `کاربر تستی «${ws.sample_username || "…"}» روی پنل‌ها ساخته شد · ${toFaDigits(n)} کانفیگ یکتا`,
        });
      } else if (errPanels) {
        setMsg({
          type: "err",
          text: `نمونه روی پنل ساخته شد ولی لینکی نیامد · ${toFaDigits(errPanels)} پنل خطا`,
        });
      } else {
        setMsg({
          type: "ok",
          text: "نمونه روی پنل ساخته شد؛ هنوز کانفیگی برنگشته — چند ثانیه بعد دوباره تازه کنید",
        });
      }
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در تازه‌سازی",
      });
    },
  });

  /** Remove probe users from remotes; keep hide rules + discovered endpoints. */
  const deleteSample = useMutation({
    mutationFn: async () => {
      if (selectedId == null) throw new Error("گروه انتخاب نشده");
      const ws = await api<SampleWorkspace>(
        `/api/group/${selectedId}/configs/sample`,
        { method: "DELETE" }
      );
      const vis = await api<VisibilityOut>(
        `/api/group/${selectedId}/config-visibility`
      );
      return { ws, vis };
    },
    onSuccess: ({ vis }) => {
      qc.setQueryData(["config-visibility", selectedId], vis);
      qc.invalidateQueries({ queryKey: ["config-visibility-all"] });
      setMsg({
        type: "ok",
        text: "کاربرهای تستی از پنل‌ها حذف شدند · تنظیمات مخفی (دامنه/پورت/هاست) حفظ شد",
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در حذف نمونه",
      });
    },
  });

  const toggle = useMutation({
    mutationFn: async (row: VisibilityItem) => {
      setPendingKey(rowKey(row, scope));
      const body = {
        host: row.host,
        port: row.port,
        match_host: (row.match_host || "").trim(),
        hidden: !row.hidden,
      };
      // Hide is always global; pick response shape for current view
      if (isAll) {
        const vis = await api<VisibilityAllOut>("/api/admin/config-visibility", {
          method: "PUT",
          body: JSON.stringify(body),
        });
        return { mode: "all" as const, vis, row };
      }
      if (selectedId == null) throw new Error("گروه انتخاب نشده");
      const vis = await api<VisibilityOut>(
        `/api/group/${selectedId}/config-visibility`,
        { method: "PUT", body: JSON.stringify(body) }
      );
      return { mode: "group" as const, vis, row };
    },
    onSuccess: (res) => {
      if (res.mode === "all") {
        qc.setQueryData(["config-visibility-all"], res.vis);
        qc.invalidateQueries({ queryKey: ["config-visibility"] });
      } else {
        qc.setQueryData(["config-visibility", selectedId], res.vis);
        qc.invalidateQueries({ queryKey: ["config-visibility-all"] });
      }
      const ep = endpointLabel(res.row);
      setMsg({
        type: "ok",
        text: res.row.hidden
          ? `${ep} دوباره در همه ساب‌ها نمایش داده می‌شود`
          : `${ep} از همه ساب‌ها (قدیم و جدید) مخفی شد`,
      });
    },
    onError: (e) => {
      setMsg({
        type: "err",
        text:
          e instanceof ApiError || e instanceof Error
            ? e.message
            : "خطا در تغییر نمایش",
      });
    },
    onSettled: () => setPendingKey(null),
  });

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return visibilityItems.filter((row) => {
      if (filter === "visible" && row.hidden) return false;
      if (filter === "hidden" && !row.hidden) return false;
      if (!needle) return true;
      const hay = [
        row.host,
        String(row.port),
        row.match_host || "",
        row.label,
        row.protocol,
        row.group_name || "",
        ...(row.panel_names || []),
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(needle);
    });
  }, [visibilityItems, q, filter]);

  const refreshing =
    refreshSample.isPending ||
    reloadAll.isPending ||
    deleteSample.isPending ||
    refreshAllSamples.isPending ||
    deleteAllSamples.isPending;
  const empty = !visLoading && visibilityItems.length === 0;
  const canDeleteSample = !isAll && sampleReady && selectedId != null;
  const canDeleteAllSamples = isAll && sampleReady;

  function selectAllPanels() {
    setScope("all");
    setMsg(null);
    setQ("");
    setFilter("all");
  }

  function selectGroup(id: number) {
    setScope("group");
    setGroupId(id);
    setMsg(null);
    setQ("");
    setFilter("all");
  }

  return (
    <div className="admin-page configs-page">
      <header className="configs-hero">
        <div className="configs-hero-text">
          <p className="configs-kicker">سابسکریپشن</p>
          <h1 className="page-title">فیلتر کانفیگ‌ها</h1>
          <p className="page-sub">
            یک گروه را انتخاب کنید و             «ساخت مجدد از پنل» (یک گروه یا همه گروه‌ها) کاربر تستی روی پنل می‌سازد
            و لیست را به‌روز می‌کند. بعد مشخص کنید کدام دامنه/پورت مخفی باشد؛{" "}
            <b>تنظیمات مخفی حفظ می‌شود</b>. «حذف تستی» فقط نمونه را از پنل
            برمی‌دارد.
          </p>
        </div>
        <div className="configs-hero-actions">
          <button
            type="button"
            className={`btn ghost configs-all-btn${isAll ? " active" : ""}`}
            disabled={refreshing || !activeGroups.length}
            onClick={selectAllPanels}
          >
            <Server size={16} />
            <span>همه پنل‌ها</span>
          </button>
          {isAll ? (
            <>
              <button
                type="button"
                className="btn configs-refresh-btn"
                disabled={refreshing || !activeGroups.length}
                onClick={() => {
                  setMsg(null);
                  refreshAllSamples.mutate();
                }}
              >
                {refreshAllSamples.isPending ? (
                  <Loader2 size={16} className="configs-spin" />
                ) : (
                  <RefreshCw size={16} />
                )}
                <span>
                  {refreshAllSamples.isPending
                    ? "در حال ساخت روی همه گروه‌ها…"
                    : sampleReady
                      ? "ساخت مجدد همه گروه‌ها از پنل"
                      : "ساخت نمونه روی همه گروه‌ها"}
                </span>
              </button>
              <button
                type="button"
                className="btn ghost"
                disabled={refreshing}
                onClick={() => {
                  setMsg(null);
                  reloadAll.mutate();
                }}
              >
                {reloadAll.isPending ? (
                  <Loader2 size={16} className="configs-spin" />
                ) : (
                  <RefreshCw size={16} />
                )}
                <span>
                  {reloadAll.isPending ? "در حال بارگذاری…" : "بارگذاری از دیتابیس"}
                </span>
              </button>
              {canDeleteAllSamples ? (
                <button
                  type="button"
                  className="btn danger configs-delete-sample-btn"
                  disabled={refreshing}
                  onClick={() => {
                    setMsg(null);
                    deleteAllSamples.mutate();
                  }}
                >
                  {deleteAllSamples.isPending ? (
                    <Loader2 size={16} className="configs-spin" />
                  ) : (
                    <Trash2 size={16} />
                  )}
                  <span>
                    {deleteAllSamples.isPending
                      ? "در حال حذف از همه پنل‌ها…"
                      : "حذف تستی همه گروه‌ها"}
                  </span>
                </button>
              ) : null}
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn configs-refresh-btn"
                disabled={refreshing || selectedId == null}
                onClick={() => {
                  setMsg(null);
                  refreshSample.mutate();
                }}
              >
                {refreshSample.isPending ? (
                  <Loader2 size={16} className="configs-spin" />
                ) : (
                  <RefreshCw size={16} />
                )}
                <span>
                  {refreshSample.isPending
                    ? "در حال ساخت روی پنل…"
                    : sampleReady
                      ? "ساخت مجدد از پنل"
                      : "ساخت نمونه روی پنل"}
                </span>
              </button>
              {canDeleteSample ? (
                <button
                  type="button"
                  className="btn danger configs-delete-sample-btn"
                  disabled={refreshing}
                  onClick={() => {
                    setMsg(null);
                    deleteSample.mutate();
                  }}
                >
                  {deleteSample.isPending ? (
                    <Loader2 size={16} className="configs-spin" />
                  ) : (
                    <Trash2 size={16} />
                  )}
                  <span>
                    {deleteSample.isPending
                      ? "در حال حذف از پنل…"
                      : "حذف کانفیگ‌های تستی"}
                  </span>
                </button>
              ) : null}
            </>
          )}
        </div>
      </header>

      {msg && (
        <p className={`configs-toast ${msg.type === "ok" ? "ok" : "error"}`} role="status">
          {msg.text}
        </p>
      )}

      <div className="configs-layout">
        <aside className="configs-side" aria-label="گروه‌ها">
          <div className="configs-side-head">
            <Layers size={15} />
            <span>گروه‌ها</span>
            <em>{toFaDigits(activeGroups.length)}</em>
          </div>
          {groupsLoading ? (
            <div className="configs-side-skel">
              <i /><i /><i />
            </div>
          ) : activeGroups.length === 0 ? (
            <p className="configs-side-empty">گروه فعالی نیست</p>
          ) : (
            <div className="configs-side-list" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={isAll}
                className={`configs-side-item configs-side-all${isAll ? " active" : ""}`}
                onClick={selectAllPanels}
              >
                <strong>
                  <Server size={14} />
                  <span>همه پنل‌ها</span>
                </strong>
                <span>کانفیگ‌های تمام گروه‌ها</span>
              </button>
              {activeGroups.map((g) => {
                const active = !isAll && g.id === selectedId;
                return (
                  <button
                    key={g.id}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    className={`configs-side-item${active ? " active" : ""}`}
                    onClick={() => selectGroup(g.id)}
                  >
                    <strong>{g.name}</strong>
                    <span>
                      {toFaDigits(g.panels?.length || 0)} پنل
                      {(g.config_tag || "").trim()
                        ? ` · ${(g.config_tag || "").trim()}`
                        : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        <section className="configs-main-panel">
          {!isAll && selectedId == null ? (
            <div className="admin-empty configs-empty">
              <Cable size={40} strokeWidth={1.35} />
              <p>یک گروه انتخاب کنید یا «همه پنل‌ها» را بزنید</p>
            </div>
          ) : (
            <>
              <div className="configs-panel-head">
                <div>
                  <h2>{isAll ? "همه پنل‌ها" : selectedGroup?.name || "گروه"}</h2>
                  <p>
                    {visFetching && !refreshing ? "در حال همگام‌سازی… · " : ""}
                    {toFaDigits(totalCount)} کانفیگ یکتا
                    {hiddenCount > 0
                      ? ` · ${toFaDigits(hiddenCount)} مخفی از ساب`
                      : ""}
                    {isAll && allVis.data
                      ? ` · ${toFaDigits(allVis.data.groups_count)} گروه`
                      : ""}
                  </p>
                </div>
                <div className="configs-panel-pills">
                  <span className={`configs-pill${sampleReady ? " on" : ""}`}>
                    <Sparkles size={12} />
                    {isAll
                      ? sampleReady
                        ? `${toFaDigits(allVis.data?.sample_ready_groups || 0)} گروه با نمونه روی پنل`
                        : "بدون نمونه فعال روی پنل"
                      : sampleReady
                        ? groupVis.data?.sample_username
                          ? `نمونه روی پنل: ${groupVis.data.sample_username}`
                          : "نمونه روی پنل فعال است"
                        : "بدون نمونه روی پنل"}
                  </span>
                  {isAll ? (
                    <button
                      type="button"
                      className="btn sm configs-rebuild-btn"
                      disabled={refreshing || !activeGroups.length}
                      onClick={() => {
                        setMsg(null);
                        refreshAllSamples.mutate();
                      }}
                    >
                      {refreshAllSamples.isPending ? (
                        <Loader2 size={14} className="configs-spin" />
                      ) : (
                        <RefreshCw size={14} />
                      )}
                      <span>
                        {refreshAllSamples.isPending
                          ? "در حال ساخت همه…"
                          : "بروزرسانی همه از پنل"}
                      </span>
                    </button>
                  ) : selectedId != null ? (
                    <button
                      type="button"
                      className="btn sm configs-rebuild-btn"
                      disabled={refreshing}
                      onClick={() => {
                        setMsg(null);
                        refreshSample.mutate();
                      }}
                    >
                      {refreshSample.isPending ? (
                        <Loader2 size={14} className="configs-spin" />
                      ) : (
                        <RefreshCw size={14} />
                      )}
                      <span>
                        {refreshSample.isPending
                          ? "در حال ساخت…"
                          : "بروزرسانی از پنل"}
                      </span>
                    </button>
                  ) : null}
                </div>
              </div>

              <div className="configs-controls">
                <div className="configs-search">
                  <Search size={16} aria-hidden />
                  <input
                    type="search"
                    dir="ltr"
                    placeholder={
                      isAll
                        ? "جستجو دامنه، پورت، هاست، گروه…"
                        : "جستجو دامنه، پورت، نام…"
                    }
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    aria-label="جستجوی کانفیگ"
                  />
                </div>
                <div className="configs-filters" role="group" aria-label="فیلتر">
                  {(
                    [
                      ["all", "همه"],
                      ["visible", "نمایش"],
                      ["hidden", "مخفی"],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      type="button"
                      className={filter === key ? "active" : ""}
                      onClick={() => setFilter(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div className={`configs-body${refreshing ? " is-busy" : ""}`}>
                {refreshing && (
                  <div className="configs-busy" aria-live="polite">
                    <Loader2 size={22} className="configs-spin" />
                    <div>
                      <strong>
                        {refreshAllSamples.isPending
                          ? "در حال ساخت نمونه روی همه گروه‌ها"
                          : deleteAllSamples.isPending
                            ? "در حال حذف نمونه‌ها از همه پنل‌ها"
                            : isAll
                              ? "در حال خواندن لیست از دیتابیس"
                              : "در حال ساخت نمونه روی پنل‌های این گروه"}
                      </strong>
                      <span>
                        {refreshAllSamples.isPending || deleteAllSamples.isPending
                          ? "گروه‌ها یکی‌یکی پردازش می‌شوند — چند دقیقه صبر کنید"
                          : isAll
                            ? "بدون تماس با پنل‌های ریموت"
                            : "فقط همین گروه — لینک‌ها جمع می‌شوند"}
                      </span>
                    </div>
                  </div>
                )}

                {visError ? (
                  <div className="admin-empty configs-empty">
                    <p>
                      {visError instanceof Error
                        ? visError.message
                        : "خطا در بارگذاری"}
                    </p>
                  </div>
                ) : visLoading ? (
                  <div className="admin-skel">
                    <div className="admin-skel-card" />
                    <div className="admin-skel-card" />
                    <div className="admin-skel-card" />
                  </div>
                ) : empty ? (
                  <div className="admin-empty configs-empty">
                    <Cable size={40} strokeWidth={1.35} />
                    <p>
                      {isAll
                        ? "هنوز کانفیگی از پنل‌ها بارگذاری نشده"
                        : "هنوز کانفیگی برای این گروه نیست"}
                    </p>
                    <p className="muted">
                      {isAll
                        ? "«ساخت نمونه روی همه گروه‌ها» برای هر گروه فعال یک کاربر تستی روی پنل می‌سازد و لیست را اینجا جمع می‌کند."
                        : "«ساخت نمونه روی پنل» یک کاربر تستی روی پنل‌های همین گروه می‌سازد و همه کانفیگ‌ها را در لیست می‌آورد."}
                    </p>
                    <button
                      type="button"
                      className="btn"
                      disabled={
                        refreshing || (isAll ? !activeGroups.length : selectedId == null)
                      }
                      onClick={() => {
                        setMsg(null);
                        if (isAll) refreshAllSamples.mutate();
                        else refreshSample.mutate();
                      }}
                    >
                      <RefreshCw size={16} />
                      <span>
                        {isAll
                          ? "ساخت نمونه روی همه گروه‌ها"
                          : "ساخت نمونه روی پنل"}
                      </span>
                    </button>
                  </div>
                ) : filtered.length === 0 ? (
                  <div className="admin-empty configs-empty">
                    <p>با این فیلتر چیزی پیدا نشد</p>
                  </div>
                ) : (
                  <div className="configs-list" role="list">
                    {filtered.map((row) => {
                      const key = rowKey(row, scope);
                      const busy = pendingKey === key && toggle.isPending;
                      const shown = !row.hidden;
                      return (
                        <article
                          key={key}
                          role="listitem"
                          className={`configs-row${row.hidden ? " is-hidden" : ""}${
                            row.orphan ? " is-orphan" : ""
                          }`}
                        >
                          <div className="configs-proto" aria-hidden>
                            {protoBadge(row.protocol)}
                          </div>
                          <div className="configs-main">
                            <div
                              className="configs-host"
                              dir="ltr"
                              title={endpointLabel(row)}
                            >
                              <span className="configs-host-name">{row.host}</span>
                              <span className="configs-port">:{row.port}</span>
                            </div>
                            <div className="configs-meta">
                              {isAll && row.group_name ? (
                                <span className="configs-group-tag">{row.group_name}</span>
                              ) : null}
                              {(row.match_host || "").trim() ? (
                                <span
                                  className="configs-match"
                                  dir="ltr"
                                  title="هاست هدر / SNI"
                                >
                                  host={(row.match_host || "").trim()}
                                </span>
                              ) : null}
                              {row.label ? (
                                <span className="configs-label">{row.label}</span>
                              ) : null}
                              {row.count > 0 ? (
                                <span>{toFaDigits(row.count)} لینک</span>
                              ) : null}
                              {row.panel_names?.length ? (
                                <span>{row.panel_names.join(" · ")}</span>
                              ) : null}
                              {row.orphan ? (
                                <span className="configs-badge warn">فقط قانون مخفی</span>
                              ) : null}
                              {row.hidden ? (
                                <span className="configs-badge err">مخفی از همه ساب‌ها</span>
                              ) : (
                                <span className="configs-badge ok">در ساب</span>
                              )}
                            </div>
                          </div>
                          <label
                            className={`configs-switch${busy ? " busy" : ""}`}
                            title={
                              shown
                                ? "الان در ساب هست — برای مخفی کردن بزنید"
                                : "مخفی است — برای نمایش بزنید"
                            }
                          >
                            <span className="configs-switch-label">
                              {shown ? (
                                <>
                                  <Eye size={14} />
                                  نمایش
                                </>
                              ) : (
                                <>
                                  <EyeOff size={14} />
                                  مخفی
                                </>
                              )}
                            </span>
                            <input
                              type="checkbox"
                              checked={shown}
                              disabled={busy || refreshing}
                              onChange={() => toggle.mutate(row)}
                              aria-label={
                                shown
                                  ? `مخفی کردن ${key} از ساب`
                                  : `نمایش ${key} در ساب`
                              }
                            />
                            <span className="configs-switch-track" aria-hidden />
                          </label>
                        </article>
                      );
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
