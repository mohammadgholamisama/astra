import { FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  BarChart3,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  Layers,
  Menu,
  Pencil,
  Plus,
  Power,
  PowerOff,
  QrCode,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  Users,
  Wifi,
  X,
} from "lucide-react";
import QRCode from "qrcode";
import Select from "../components/Select";
import {
  ApiError,
  api,
  bytesToGbInput,
  expireToDaysInput,
  gbToBytes,
  Group,
  PanelAccount,
  User,
} from "../lib/api";
import {
  formatDataLimit,
  formatExpire,
  formatTraffic,
  remainingBytes,
  toFaDigits,
  usagePercent,
} from "../lib/format";

type FilterKey = "all" | "active" | "error" | "expired";

const PAGE_SIZE = 10;

function userHasErrors(u: User) {
  return (u.panel_accounts || []).some((a) => a.status === "error" || !!a.error_message);
}

function userIsExpired(u: User) {
  const ts = Number(u.expire) || 0;
  return ts > 0 && ts <= Math.floor(Date.now() / 1000);
}

function statusLabel(u: User) {
  if (u.status === "disabled") return { text: "خاموش", tone: "muted" as const };
  if (u.status === "limited") return { text: "اتمام حجم", tone: "warn" as const };
  if (u.status === "expired" || userIsExpired(u)) return { text: "منقضی", tone: "danger" as const };
  if (u.status === "on_hold") return { text: "معلق", tone: "muted" as const };
  if (u.status === "active") return { text: "فعال", tone: "ok" as const };
  return { text: u.status || "—", tone: "muted" as const };
}

type PanelUsageRow = {
  panelId: number;
  name: string;
  raw: number;
  coefficient: number;
  weighted: number;
  remoteLimit: number;
  configs: number;
};

function coeffsFromGroups(groups: Group[], groupIds: number[]): Map<number, number> {
  const want = new Set(groupIds || []);
  const map = new Map<number, number>();
  for (const g of groups) {
    if (want.size && !want.has(g.id)) continue;
    for (const gp of g.panels || []) {
      const raw = Number((gp.config as { coefficient?: number } | undefined)?.coefficient ?? 1);
      const c = !Number.isNaN(raw) && raw >= 0.01 ? raw : 1;
      const prev = map.get(gp.panel_id);
      map.set(gp.panel_id, prev == null ? c : Math.max(prev, c));
    }
  }
  return map;
}

function accountWeighted(a: User["panel_accounts"][number], coefficient: number): number {
  const raw = Number(a.used_traffic) || 0;
  const anchorRaw = Number(a.coeff_anchor_raw) || 0;
  const anchorW = Number(a.coeff_anchor_weighted) || 0;
  const coeff = coefficient >= 0.01 ? coefficient : 1;
  if (raw < anchorRaw) return Math.round(raw * coeff);
  // Past traffic stays at old rate; only delta uses live coefficient
  return anchorW + Math.round((raw - anchorRaw) * coeff);
}

function panelUsageRows(u: User, groups: Group[]): PanelUsageRow[] {
  const coeffs = coeffsFromGroups(groups, u.group_ids || []);
  const astraLimit = Number(u.data_limit) || 0;
  const usedWeighted = Number(u.used_traffic) || 0;
  const rows: PanelUsageRow[] = [];

  for (const a of u.panel_accounts || []) {
    if (a.status !== "active" && a.status !== "error") continue;
    const raw = Number(a.used_traffic) || 0;
    const coefficient = coeffs.get(a.panel_id) ?? 1;
    const weighted = accountWeighted(a, coefficient);
    // Shared remaining: panel ceiling = raw_used + (astra_remaining / coeff)
    let remoteLimit = 0;
    if (astraLimit > 0) {
      const remaining = Math.max(0, astraLimit - usedWeighted);
      remoteLimit =
        remaining <= 0
          ? Math.max(1, raw || 1)
          : Math.max(1, raw + Math.floor(remaining / coefficient));
    }
    rows.push({
      panelId: a.panel_id,
      name: a.panel_name || `پنل #${a.panel_id}`,
      raw,
      coefficient,
      weighted,
      remoteLimit,
      configs: (a.links || []).filter(Boolean).length,
    });
  }
  rows.sort((a, b) => b.weighted - a.weighted);
  return rows;
}

export default function UsersPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [filterGroupId, setFilterGroupId] = useState<number | "">("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [dataGb, setDataGb] = useState("50");
  const [days, setDays] = useState("30");
  const [groupId, setGroupId] = useState<number | "">("");
  const [copied, setCopied] = useState("");
  const [confirmUser, setConfirmUser] = useState<User | null>(null);
  const [resetUser, setResetUser] = useState<User | null>(null);
  const [editUser, setEditUser] = useState<User | null>(null);
  const [editDisplayName, setEditDisplayName] = useState("");
  const [editDataGb, setEditDataGb] = useState("50");
  const [editDays, setEditDays] = useState("30");
  const [errorsUser, setErrorsUser] = useState<User | null>(null);
  const [subUser, setSubUser] = useState<User | null>(null);
  const [usageUser, setUsageUser] = useState<User | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [qrDataUrl, setQrDataUrl] = useState("");
  const [actionMsg, setActionMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [syncingUsage, setSyncingUsage] = useState(false);
  const [menuUser, setMenuUser] = useState<User | null>(null);

  useEffect(() => {
    if (!menuUser) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuUser(null);
    }
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      document.removeEventListener("keydown", onKey);
    };
  }, [menuUser]);

  const [searchDebounced, setSearchDebounced] = useState(search);
  useEffect(() => {
    const t = window.setTimeout(() => setSearchDebounced(search), 280);
    return () => window.clearTimeout(t);
  }, [search]);

  useEffect(() => {
    setPage(0);
  }, [searchDebounced, filterGroupId]);

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ["users", searchDebounced, filterGroupId, page],
    queryFn: () => {
      const qs = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(page * PAGE_SIZE),
        // Fast list: no live remote pull (use «همگام‌سازی مصرف» for that)
        refresh: "false",
      });
      if (searchDebounced.trim()) qs.set("search", searchDebounced.trim());
      if (filterGroupId !== "") qs.set("group_id", String(filterGroupId));
      return api<{ users: User[]; total: number }>(`/api/users?${qs}`);
    },
    staleTime: 30_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    placeholderData: (prev) => prev,
  });

  async function syncUsageNow() {
    setSyncingUsage(true);
    try {
      const qs = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(page * PAGE_SIZE),
        refresh: "true",
      });
      if (searchDebounced.trim()) qs.set("search", searchDebounced.trim());
      if (filterGroupId !== "") qs.set("group_id", String(filterGroupId));
      const fresh = await api<{ users: User[]; total: number }>(`/api/users?${qs}`);
      qc.setQueryData(["users", searchDebounced, filterGroupId, page], fresh);
      setActionMsg({ type: "ok", text: "مصرف این صفحه همگام شد" });
    } catch (e) {
      setActionMsg({
        type: "err",
        text: e instanceof Error ? e.message : "همگام‌سازی ناموفق",
      });
    } finally {
      setSyncingUsage(false);
    }
  }
  const { data: groups = [] } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<Group[]>("/api/groups"),
    staleTime: 60_000,
  });

  const create = useMutation({
    mutationFn: () => {
      const gb = Number(dataGb);
      const dayN = Number(days);
      const expire =
        dayN > 0 ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400) : 0;
      const dn = displayName.trim();
      return api<User>("/api/user", {
        method: "POST",
        body: JSON.stringify({
          username,
          display_name: dn || null,
          data_limit: gb > 0 ? gbToBytes(gb) : 0,
          expire,
          status: "active",
          group_ids: [Number(groupId)],
        }),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setOpen(false);
      setUsername("");
      setDisplayName("");
      setDataGb("50");
      setDays("30");
      setGroupId("");
      setActionMsg({ type: "ok", text: "کاربر ساخته شد" });
    },
  });

  const createErrorText = (() => {
    if (!create.isError) return null;
    const err = create.error;
    if (err instanceof ApiError) {
      if (err.code === "USERNAME_EXISTS" || err.status === 409) {
        return "این یوزرنیم قبلاً ثبت شده است — یوزرنیم دیگری انتخاب کنید";
      }
      return err.message;
    }
    return err instanceof Error ? err.message : "خطا در ساخت کاربر";
  })();

  const remove = useMutation({
    mutationFn: (u: string) => api(`/api/user/${u}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setConfirmUser(null);
      setActionMsg({ type: "ok", text: "کاربر از همه پنل‌ها حذف شد" });
    },
    onError: (e: Error) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setConfirmUser(null);
      setActionMsg({ type: "err", text: e.message });
    },
  });

  const retry = useMutation({
    mutationFn: (u: string) => api(`/api/user/${u}/retry`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setActionMsg({ type: "ok", text: "تلاش مجدد انجام شد" });
    },
    onError: (e: Error) => setActionMsg({ type: "err", text: e.message }),
  });

  const togglePower = useMutation({
    mutationFn: ({ username, enable }: { username: string; enable: boolean }) =>
      api<User>(`/api/user/${encodeURIComponent(username)}`, {
        method: "PUT",
        body: JSON.stringify({ status: enable ? "active" : "disabled" }),
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setActionMsg({
        type: "ok",
        text: vars.enable
          ? "کاربر روشن شد — کانفیگ‌ها روی پنل‌ها فعال شدند"
          : "کاربر خاموش شد — کانفیگ‌ها روی همه پنل‌ها غیرفعال شدند",
      });
    },
    onError: (e: Error) => setActionMsg({ type: "err", text: e.message }),
  });

  const resetTraffic = useMutation({
    mutationFn: (username: string) =>
      api<User>(`/api/user/${encodeURIComponent(username)}/reset`, {
        method: "POST",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setResetUser(null);
      setActionMsg({
        type: "ok",
        text: "مصرف بازنشانی شد — روی Astra و پنل‌های اصلی صفر شد",
      });
    },
    onError: (e: Error) => {
      setResetUser(null);
      setActionMsg({ type: "err", text: e.message });
    },
  });

  const saveEdit = useMutation({
    mutationFn: () => {
      if (!editUser) throw new Error("کاربر انتخاب نشده");
      const gb = Number(editDataGb);
      const dayN = Number(editDays);
      const expire =
        dayN > 0 ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400) : 0;
      const dn = editDisplayName.trim();
      return api<User>(`/api/user/${encodeURIComponent(editUser.username)}`, {
        method: "PUT",
        body: JSON.stringify({
          display_name: dn || null,
          clear_display_name: !dn,
          data_limit: gb > 0 ? gbToBytes(gb) : 0,
          expire,
        }),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setEditUser(null);
      setActionMsg({ type: "ok", text: "کاربر به‌روز شد — تغییرات روی پنل‌ها اعمال شد" });
    },
    onError: (e: Error) => setActionMsg({ type: "err", text: e.message }),
  });

  function openEdit(u: User) {
    setActionMsg(null);
    setEditUser(u);
    setEditDisplayName(u.display_name || "");
    setEditDataGb(bytesToGbInput(u.data_limit));
    setEditDays(expireToDaysInput(u.expire));
  }

  useEffect(() => {
    if (!subUser?.subscription_url) {
      setQrDataUrl("");
      return;
    }
    let cancelled = false;
    QRCode.toDataURL(subUser.subscription_url, {
      width: 280,
      margin: 2,
      color: { dark: "#0f3d38", light: "#ffffff" },
      errorCorrectionLevel: "M",
    })
      .then((url) => {
        if (!cancelled) setQrDataUrl(url);
      })
      .catch(() => {
        if (!cancelled) setQrDataUrl("");
      });
    return () => {
      cancelled = true;
    };
  }, [subUser]);

  function copy(text: string, key: string) {
    void navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(""), 1600);
  }

  async function openUsage(u: User) {
    setUsageUser(u);
    setUsageLoading(true);
    try {
      const fresh = await api<User>(`/api/user/${encodeURIComponent(u.username)}`);
      setUsageUser(fresh);
      qc.invalidateQueries({ queryKey: ["users"] });
    } catch {
      /* keep list snapshot */
    } finally {
      setUsageLoading(false);
    }
  }

  const usageRows = useMemo(
    () => (usageUser ? panelUsageRows(usageUser, groups) : []),
    [usageUser, groups]
  );
  const usageTotalWeighted = useMemo(
    () => usageRows.reduce((s, r) => s + r.weighted, 0),
    [usageRows]
  );
  const usageMaxWeighted = useMemo(
    () => Math.max(1, ...usageRows.map((r) => r.weighted), 1),
    [usageRows]
  );

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (groupId === "") return;
    create.mutate();
  }

  const users = data?.users || [];
  const totalUsers = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalUsers / PAGE_SIZE));
  const groupsById = useMemo(() => {
    const m = new Map<number, Group>();
    for (const g of groups) m.set(g.id, g);
    return m;
  }, [groups]);

  const stats = useMemo(() => {
    const total = totalUsers;
    let active = 0;
    let withError = 0;
    let expired = 0;
    let expiringSoon = 0;
    let usedSum = 0;
    let limited = 0;
    const now = Math.floor(Date.now() / 1000);
    for (const u of users) {
      usedSum += Number(u.used_traffic) || 0;
      if ((Number(u.data_limit) || 0) > 0) limited += 1;
      if (userHasErrors(u)) withError += 1;
      if (userIsExpired(u)) expired += 1;
      else if (u.status === "active") active += 1;
      const exp = Number(u.expire) || 0;
      if (exp > now && exp - now <= 7 * 86400) expiringSoon += 1;
    }
    return { total, active, withError, expired, expiringSoon, usedSum, limited, loaded: users.length };
  }, [users, totalUsers]);

  const filtered = useMemo(() => {
    return users.filter((u) => {
      if (filter === "active") return u.status === "active" && !userIsExpired(u);
      if (filter === "error") return userHasErrors(u);
      if (filter === "expired") return userIsExpired(u);
      return true;
    });
  }, [users, filter]);

  useEffect(() => {
    if (page > 0 && page >= totalPages) setPage(Math.max(0, totalPages - 1));
  }, [page, totalPages]);

  const errorAccounts: PanelAccount[] = useMemo(() => {
    if (!errorsUser) return [];
    return (errorsUser.panel_accounts || []).filter(
      (a) => a.status === "error" || !!a.error_message
    );
  }, [errorsUser]);

  const filterGroupName =
    filterGroupId === "" ? null : groupsById.get(Number(filterGroupId))?.name || null;

  const filters: { key: FilterKey; label: string; count: number }[] = [
    { key: "all", label: "همه", count: stats.loaded },
    { key: "active", label: "فعال", count: stats.active },
    { key: "error", label: "خطا", count: stats.withError },
    { key: "expired", label: "منقضی", count: stats.expired },
  ];

  const hasExtraFilter = !!search.trim() || filter !== "all" || filterGroupId !== "";

  return (
    <div className="users-page">
      <header className="users-head">
        <div>
          <h1 className="page-title">کاربران</h1>
          <p className="page-sub">
            ساخت کانفیگ روی پنل‌های گروه · لینک ساب تجمیعی
            {isFetching && !syncingUsage ? " · در حال بارگذاری…" : ""}
            {syncingUsage ? " · همگام‌سازی مصرف…" : ""}
          </p>
        </div>
        <div className="users-head-actions">
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => void syncUsageNow()}
            disabled={isFetching || syncingUsage}
            title="مصرف زنده همین صفحه از پنل‌ها"
          >
            <RefreshCw size={14} className={syncingUsage ? "spin" : undefined} />
            {syncingUsage ? "همگام‌سازی…" : "تازه‌سازی مصرف"}
          </button>
          <button type="button" className="btn users-add" onClick={() => setOpen(true)}>
            <Plus size={16} />
            کاربر جدید
          </button>
        </div>
      </header>

      <section className="users-stats" aria-label="آمار کاربران">
        <article className="users-stat">
          <span className="users-stat-icon">
            <Users size={18} />
          </span>
          <div>
            <strong>{toFaDigits(stats.total)}</strong>
            <span>{filterGroupName ? `در «${filterGroupName}»` : "کل کاربران"}</span>
          </div>
        </article>
        <article className="users-stat ok">
          <span className="users-stat-icon">
            <Wifi size={18} />
          </span>
          <div>
            <strong>
              {toFaDigits(stats.active)}
              <small>/{toFaDigits(stats.loaded)}</small>
            </strong>
            <span>فعال</span>
          </div>
        </article>
        <article className={`users-stat ${stats.withError ? "danger" : ""}`}>
          <span className="users-stat-icon">
            <AlertTriangle size={18} />
          </span>
          <div>
            <strong>{toFaDigits(stats.withError)}</strong>
            <span>خطای پنل</span>
          </div>
        </article>
        <article className={`users-stat ${stats.expiringSoon ? "warn" : ""}`}>
          <span className="users-stat-icon">
            <Clock size={18} />
          </span>
          <div>
            <strong>{toFaDigits(stats.expiringSoon)}</strong>
            <span>انقضا تا ۷ روز</span>
          </div>
        </article>
        <article className="users-stat wide">
          <span className="users-stat-icon">
            <BarChart3 size={18} />
          </span>
          <div>
            <strong>{formatTraffic(stats.usedSum, { empty: "۰" })}</strong>
            <span>
              مصرف · {toFaDigits(stats.limited)} کاربر با سقف حجم
            </span>
          </div>
        </article>
      </section>

      <div className="users-toolbar">
        <div className="users-toolbar-row">
          <div className="users-search">
            <Search size={16} />
            <input
              placeholder="جستجوی یوزرنیم…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              dir="ltr"
            />
          </div>
          <div className="users-group-filter">
            <Layers size={16} aria-hidden />
            <Select
              value={filterGroupId}
              placeholder="همه گروه‌ها"
              options={groups.map((g) => ({
                value: g.id,
                label: g.name,
                hint: `${g.panels?.length || 0} پنل`,
              }))}
              onChange={(v) => setFilterGroupId(v)}
            />
            {filterGroupId !== "" && (
              <button
                type="button"
                className="btn ghost sm users-group-clear"
                onClick={() => setFilterGroupId("")}
                title="حذف فیلتر گروه"
                aria-label="حذف فیلتر گروه"
              >
                <X size={14} />
              </button>
            )}
          </div>
        </div>
        <div className="users-toolbar-row users-toolbar-bottom">
          <div className="users-filters" role="tablist">
            {filters.map((f) => (
              <button
                key={f.key}
                type="button"
                role="tab"
                aria-selected={filter === f.key}
                className={`users-filter ${filter === f.key ? "active" : ""}`}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
                <em>{toFaDigits(f.count)}</em>
              </button>
            ))}
          </div>
          {hasExtraFilter && (
            <button
              type="button"
              className="btn ghost sm users-clear-filters"
              onClick={() => {
                setSearch("");
                setFilter("all");
                setFilterGroupId("");
              }}
            >
              پاک‌کردن فیلترها
            </button>
          )}
        </div>
      </div>

      {actionMsg && (
        <div className={`users-toast ${actionMsg.type}`}>
          <span>{actionMsg.text}</span>
          <button type="button" className="btn ghost sm" onClick={() => setActionMsg(null)}>
            بستن
          </button>
        </div>
      )}

      {isLoading ? (
        <div className="users-skel">
          <div className="users-skel-card" />
          <div className="users-skel-card" />
          <div className="users-skel-card" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="users-empty">
          <Users size={36} strokeWidth={1.4} />
          <p>
            {hasExtraFilter
              ? "با این فیلتر نتیجه‌ای پیدا نشد"
              : "هنوز کاربری نیست"}
          </p>
          {!hasExtraFilter && (
            <button className="btn" onClick={() => setOpen(true)}>
              ساخت اولین کاربر
            </button>
          )}
        </div>
      ) : (
        <>
        <div className="users-table" role="list">
          <div className="users-table-head" aria-hidden>
            <span>کاربر</span>
            <span>گروه</span>
            <span>حجم</span>
            <span>انقضا</span>
            <span>پنل</span>
            <span />
          </div>
          {filtered.map((u) => {
            const errors = (u.panel_accounts || []).filter(
              (a) => a.status === "error" || !!a.error_message
            );
            const ok = (u.panel_accounts || []).filter((a) => a.status === "active").length;
            const panels = (u.panel_accounts || []).length;
            const pct = usagePercent(u.used_traffic, u.data_limit);
            const remain = remainingBytes(u.used_traffic, u.data_limit);
            const exp = formatExpire(u.expire);
            const st = statusLabel(u);
            const groupNames = (u.group_ids || [])
              .map((id) => groupsById.get(id)?.name)
              .filter(Boolean)
              .join("، ");
            const barTone =
              pct == null ? "muted" : pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
            const isOff = u.status === "disabled";
            const menuOpen = menuUser?.username === u.username;

            return (
              <article
                key={u.username}
                role="listitem"
                className={`user-row${errors.length ? " has-err" : ""}${isOff ? " is-off" : ""}${menuOpen ? " menu-open" : ""}`}
              >
                <div className="user-row-user">
                  <div className="user-row-name">
                    <b dir="ltr">{u.username}</b>
                    <span className={`badge ${st.tone}`}>{st.text}</span>
                  </div>
                  {u.display_name ? (
                    <div className="user-row-display" title="نام کانفیگ‌ها">
                      {u.display_name}
                    </div>
                  ) : null}
                </div>

                <div className="user-row-group" title={groupNames || undefined}>
                  {groupNames ? (
                    <span className="user-group-chip">
                      <Layers size={12} />
                      {groupNames}
                    </span>
                  ) : (
                    <span className="muted">بدون گروه</span>
                  )}
                </div>

                <div className={`user-row-usage ${barTone}`}>
                  <div className="user-row-usage-top">
                    <span>
                      {formatTraffic(u.used_traffic, { empty: "۰" })}
                      <em> / {formatDataLimit(u.data_limit)}</em>
                    </span>
                    <strong>{pct == null ? "∞" : `${toFaDigits(pct)}٪`}</strong>
                  </div>
                  <div className="user-bar" aria-hidden>
                    <div
                      className={`user-bar-fill ${barTone}`}
                      style={{ width: pct == null ? "0%" : `${Math.min(100, pct)}%` }}
                    />
                  </div>
                  <div className="user-row-usage-foot">
                    {remain != null
                      ? `باقی: ${formatTraffic(remain, { empty: "۰" })}`
                      : "نامحدود"}
                  </div>
                </div>

                <div className={`user-row-expire ${exp.tone}`}>
                  <Clock size={13} aria-hidden />
                  <div>
                    <strong>{exp.label}</strong>
                    <span>
                      {exp.daysLeft == null
                        ? "بدون انقضا"
                        : exp.daysLeft === 0
                          ? "تمام‌شده"
                          : `${toFaDigits(exp.daysLeft)} روز`}
                    </span>
                  </div>
                </div>

                <div className="user-row-panels">
                  <span className="badge ok">{toFaDigits(ok)}/{toFaDigits(panels)}</span>
                  {errors.length > 0 && (
                    <button
                      type="button"
                      className="badge err user-err-btn"
                      onClick={() => setErrorsUser(u)}
                    >
                      {toFaDigits(errors.length)} خطا
                    </button>
                  )}
                </div>

                <div className="user-row-menu">
                  <button
                    type="button"
                    className={`user-menu-btn${menuOpen ? " open" : ""}`}
                    aria-label="منوی عملیات"
                    aria-expanded={menuOpen}
                    aria-haspopup="dialog"
                    onClick={() =>
                      setMenuUser((cur) => (cur?.username === u.username ? null : u))
                    }
                  >
                    <Menu size={18} strokeWidth={2.2} />
                  </button>
                </div>
              </article>
            );
          })}
        </div>
        {totalUsers > 0 && (
          <nav className="users-pagination" aria-label="صفحه‌بندی کاربران">
            <button
              type="button"
              className="users-page-btn"
              disabled={page <= 0 || isFetching}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              aria-label="صفحه قبلی"
            >
              <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
              <span>قبلی</span>
            </button>
            <div className="users-pagination-mid">
              <span className="users-page-chip">
                {toFaDigits(page + 1)}
                <span className="users-page-chip-sep">/</span>
                {toFaDigits(totalPages)}
              </span>
              <span className="users-pagination-range">
                {toFaDigits(page * PAGE_SIZE + 1)}–
                {toFaDigits(Math.min((page + 1) * PAGE_SIZE, totalUsers))} از{" "}
                {toFaDigits(totalUsers)} کاربر
              </span>
            </div>
            <button
              type="button"
              className="users-page-btn"
              disabled={page + 1 >= totalPages || isFetching}
              onClick={() => setPage((p) => p + 1)}
              aria-label="صفحه بعدی"
            >
              <span>بعدی</span>
              <ChevronLeft size={15} strokeWidth={2.25} aria-hidden />
            </button>
          </nav>
        )}
        </>
      )}

      {menuUser && (() => {
        const u = menuUser;
        const st = statusLabel(u);
        const isOff = u.status === "disabled";
        const errors = (u.panel_accounts || []).filter(
          (a) => a.status === "error" || !!a.error_message
        );
        const ok = (u.panel_accounts || []).filter((a) => a.status === "active").length;
        const panels = (u.panel_accounts || []).length;
        const remain = remainingBytes(u.used_traffic, u.data_limit);
        const exp = formatExpire(u.expire);
        const groupNames = (u.group_ids || [])
          .map((id) => groupsById.get(id)?.name)
          .filter(Boolean)
          .join("، ");
        return (
          <div
            className="modal-backdrop user-actions-backdrop"
            onClick={() => setMenuUser(null)}
          >
            <div
              className="modal user-actions-sheet"
              role="dialog"
              aria-modal="true"
              aria-labelledby="user-actions-title"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="user-actions-handle" aria-hidden />
              <header className="user-actions-head">
                <div className="user-actions-who">
                  <div className="user-actions-name">
                    <b id="user-actions-title" dir="ltr">
                      {u.username}
                    </b>
                    <span className={`badge ${st.tone}`}>{st.text}</span>
                  </div>
                  {u.display_name ? (
                    <p className="user-actions-display">{u.display_name}</p>
                  ) : null}
                  <div className="user-actions-meta">
                    {groupNames ? (
                      <span>
                        <Layers size={12} />
                        {groupNames}
                      </span>
                    ) : null}
                    <span>
                      <Wifi size={12} />
                      {toFaDigits(ok)}/{toFaDigits(panels)} پنل
                    </span>
                    <span>
                      باقی{" "}
                      {remain != null
                        ? formatTraffic(remain, { empty: "۰" })
                        : "نامحدود"}
                    </span>
                    <span className={exp.tone}>
                      <Clock size={12} />
                      {exp.label}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  className="modal-close"
                  onClick={() => setMenuUser(null)}
                  aria-label="بستن"
                >
                  <X size={18} />
                </button>
              </header>

              <div className="user-actions-list" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuUser(null);
                    setSubUser(u);
                  }}
                >
                  <span className="user-actions-ico">
                    <QrCode size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>لینک ساب</strong>
                    <small>QR و کپی لینک اشتراک</small>
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuUser(null);
                    openEdit(u);
                  }}
                >
                  <span className="user-actions-ico">
                    <Pencil size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>ویرایش</strong>
                    <small>حجم، روز و نام کانفیگ</small>
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuUser(null);
                    void openUsage(u);
                  }}
                >
                  <span className="user-actions-ico">
                    <BarChart3 size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>مصرف پنل‌ها</strong>
                    <small>جزئیات حجم با ضریب</small>
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={togglePower.isPending}
                  onClick={() => {
                    setMenuUser(null);
                    togglePower.mutate({ username: u.username, enable: isOff });
                  }}
                >
                  <span className="user-actions-ico">
                    {isOff ? <Power size={18} /> : <PowerOff size={18} />}
                  </span>
                  <span className="user-actions-label">
                    <strong>{isOff ? "روشن کردن" : "خاموش کردن"}</strong>
                    <small>{isOff ? "فعال‌سازی کانفیگ" : "قطع دسترسی کاربر"}</small>
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={resetTraffic.isPending}
                  onClick={() => {
                    setMenuUser(null);
                    setActionMsg(null);
                    setResetUser(u);
                  }}
                >
                  <span className="user-actions-ico">
                    <RotateCcw size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>بازنشانی مصرف</strong>
                    <small>صفر کردن حجم مصرف‌شده</small>
                  </span>
                </button>
                {errors.length > 0 && (
                  <button
                    type="button"
                    role="menuitem"
                    disabled={retry.isPending}
                    onClick={() => {
                      setMenuUser(null);
                      retry.mutate(u.username);
                    }}
                  >
                    <span className="user-actions-ico warn">
                      <RefreshCw size={18} />
                    </span>
                    <span className="user-actions-label">
                      <strong>تلاش مجدد</strong>
                      <small>{toFaDigits(errors.length)} خطای پنل</small>
                    </span>
                  </button>
                )}
              </div>

              <div className="user-actions-danger">
                <button
                  type="button"
                  role="menuitem"
                  className="danger"
                  onClick={() => {
                    setMenuUser(null);
                    setActionMsg(null);
                    setConfirmUser(u);
                  }}
                >
                  <span className="user-actions-ico danger">
                    <Trash2 size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>حذف کاربر</strong>
                    <small>از Astra و همه پنل‌ها</small>
                  </span>
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {open && (
        <div className="modal-backdrop" onClick={() => !create.isPending && setOpen(false)}>
          <div className="modal user-create-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>ساخت کاربر</h3>
                <p className="modal-sub">کانفیگ روی همه پنل‌های گروه ساخته می‌شود</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form onSubmit={onSubmit} className="user-create-form">
              <div className="field">
                <label>یوزرنیم</label>
                <input
                  required
                  pattern="[a-zA-Z0-9_]{3,32}"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="مثلاً user_01"
                  dir="ltr"
                  autoFocus
                />
              </div>
              <div className="field">
                <label>نام کانفیگ‌ها</label>
                <input
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  placeholder="مثلاً علی · اختیاری"
                  maxLength={64}
                />
                <p className="field-hint">نامی که در کلاینت‌ها روی کانفیگ‌ها دیده می‌شود</p>
              </div>
              <div className="field">
                <label>گروه</label>
                <Select
                  value={groupId}
                  placeholder="گروه را انتخاب کنید"
                  options={groups.map((g) => ({
                    value: g.id,
                    label: g.name,
                    hint: `${g.panels?.length || 0} پنل`,
                  }))}
                  onChange={(v) => setGroupId(v)}
                />
              </div>
              <div className="user-create-grid">
                <div className="field">
                  <label>حجم (GB)</label>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    inputMode="decimal"
                    value={dataGb}
                    onChange={(e) => setDataGb(e.target.value)}
                    dir="ltr"
                    placeholder="0.1"
                  />
                  <p className="field-hint">۰ = نامحدود · اعشار مجاز (مثلاً ۰.۱)</p>
                </div>
                <div className="field">
                  <label>روز اعتبار</label>
                  <input
                    type="number"
                    min={0}
                    step={1}
                    inputMode="numeric"
                    value={days}
                    onChange={(e) => setDays(e.target.value)}
                    dir="ltr"
                    placeholder="30"
                  />
                  <p className="field-hint">۰ = نامحدود</p>
                </div>
              </div>
              {createErrorText && <p className="error">{createErrorText}</p>}
              <footer className="modal-foot">
                <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
                  انصراف
                </button>
                <button className="btn" disabled={create.isPending || groupId === ""}>
                  {create.isPending ? "در حال ساخت…" : "ساخت روی همه پنل‌ها"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {editUser && (
        <div
          className="modal-backdrop"
          onClick={() => !saveEdit.isPending && setEditUser(null)}
        >
          <div className="modal user-create-modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>ویرایش کاربر</h3>
                <p className="modal-sub">
                  <strong dir="ltr">{editUser.username}</strong>
                  {" · "}
                  نام کانفیگ، حجم و مدت روی همه پنل‌ها اعمال می‌شود
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setEditUser(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form
              className="user-create-form"
              onSubmit={(e) => {
                e.preventDefault();
                saveEdit.mutate();
              }}
            >
              <div className="field">
                <label>نام کانفیگ‌ها</label>
                <input
                  value={editDisplayName}
                  onChange={(e) => setEditDisplayName(e.target.value)}
                  placeholder="خالی = نام پیش‌فرض پنل"
                  maxLength={64}
                  autoFocus
                />
                <p className="field-hint">در ساب و کلاینت‌ها به‌جای نام پیش‌فرض نشان داده می‌شود</p>
              </div>
              <div className="user-create-grid">
                <div className="field">
                  <label>حجم (GB)</label>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    inputMode="decimal"
                    value={editDataGb}
                    onChange={(e) => setEditDataGb(e.target.value)}
                    dir="ltr"
                    placeholder="0.1"
                  />
                  <p className="field-hint">۰ = نامحدود</p>
                </div>
                <div className="field">
                  <label>روز اعتبار از الان</label>
                  <input
                    type="number"
                    min={0}
                    step={1}
                    inputMode="numeric"
                    value={editDays}
                    onChange={(e) => setEditDays(e.target.value)}
                    dir="ltr"
                    placeholder="30"
                  />
                  <p className="field-hint">۰ = نامحدود · از لحظه ذخیره محاسبه می‌شود</p>
                </div>
              </div>
              {saveEdit.isError && (
                <p className="error">
                  {saveEdit.error instanceof Error
                    ? saveEdit.error.message
                    : "خطا در ذخیره"}
                </p>
              )}
              <footer className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setEditUser(null)}
                  disabled={saveEdit.isPending}
                >
                  انصراف
                </button>
                <button className="btn" disabled={saveEdit.isPending}>
                  {saveEdit.isPending ? "در حال ذخیره…" : "ذخیره تغییرات"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {subUser && (
        <div className="modal-backdrop sheet-backdrop" onClick={() => setSubUser(null)}>
          <div className="modal sub-modal sheet-modal" onClick={(e) => e.stopPropagation()}>
            <div className="user-actions-handle mobile-only" aria-hidden />
            <header className="modal-head">
              <div>
                <h3>لینک ساب</h3>
                <p className="modal-sub">
                  <strong dir="ltr">{subUser.username}</strong>
                  {subUser.display_name ? ` · ${subUser.display_name}` : ""}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setSubUser(null)}
                aria-label="بستن"
              >
                <X size={18} />
              </button>
            </header>

            <div className="sub-qr-wrap">
              {qrDataUrl ? (
                <img src={qrDataUrl} alt="QR subscription" className="sub-qr" />
              ) : (
                <div className="sub-qr-skel">در حال ساخت QR…</div>
              )}
            </div>

            <div className="sub-url" dir="ltr" title={subUser.subscription_url}>
              {subUser.subscription_url}
            </div>

            <div className="sub-actions">
              <button
                type="button"
                className="btn"
                onClick={() => copy(subUser.subscription_url, "sub")}
              >
                {copied === "sub" ? <Check size={16} /> : <Copy size={16} />}
                {copied === "sub" ? "کپی شد" : "کپی لینک"}
              </button>
              <a
                className="btn ghost"
                href={subUser.subscription_url}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink size={16} />
                باز کردن
              </a>
            </div>
          </div>
        </div>
      )}

      {usageUser && (
        <div className="modal-backdrop sheet-backdrop" onClick={() => setUsageUser(null)}>
          <div
            className="modal modal-lg usage-modal sheet-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="user-actions-handle mobile-only" aria-hidden />
            <header className="modal-head">
              <div>
                <h3>مصرف پنل‌ها</h3>
                <p className="modal-sub">
                  <strong dir="ltr">{usageUser.username}</strong>
                  {usageUser.display_name ? ` · ${usageUser.display_name}` : ""}
                  {usageLoading ? " · همگام‌سازی…" : ""}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setUsageUser(null)}
                aria-label="بستن"
              >
                <X size={18} />
              </button>
            </header>

            <p className="usage-modal-hint">
              مصرف کل = جمع (خام هر پنل × ضریب). با رسیدن به سقف خرید، وضعیت «اتمام حجم» می‌شود و کانفیگ
              روی پنل اصلی هم خاموش می‌شود — حتی اگر آنجا حجم مانده باشد.
            </p>

            <div className="usage-modal-total">
              <span>مصرف کل (با ضریب)</span>
              <strong>
                {formatTraffic(usageUser.used_traffic || usageTotalWeighted, { empty: "۰" })}
                <small> / {formatDataLimit(usageUser.data_limit)}</small>
              </strong>
            </div>

            {usageRows.length === 0 ? (
              <p className="muted" style={{ textAlign: "center", padding: "20px 0" }}>
                پنل فعالی برای این کاربر نیست
              </p>
            ) : (
              <div className="usage-panel-list">
                {usageRows.map((row) => {
                  const width = Math.min(100, (row.weighted / usageMaxWeighted) * 100);
                  const share =
                    usageTotalWeighted > 0
                      ? Math.round((row.weighted / usageTotalWeighted) * 100)
                      : 0;
                  const coeffLabel =
                    row.coefficient === Math.floor(row.coefficient)
                      ? String(row.coefficient)
                      : row.coefficient.toFixed(2).replace(/\.?0+$/, "");
                  return (
                    <div key={row.panelId} className="usage-panel-row">
                      <div className="usage-panel-head">
                        <strong>{row.name}</strong>
                        <span>{formatTraffic(row.weighted, { empty: "۰" })}</span>
                      </div>
                      <div className="usage-panel-bar" aria-hidden>
                        <i style={{ width: `${width}%` }} />
                      </div>
                      <div className="usage-panel-meta">
                        <span>خام: {formatTraffic(row.raw, { empty: "۰" })}</span>
                        <span>ضریب ×{toFaDigits(coeffLabel)}</span>
                        <span>
                          روی پنل:{" "}
                          {row.remoteLimit > 0
                            ? formatTraffic(row.remoteLimit, { empty: "۰" })
                            : "نامحدود"}
                        </span>
                        <span>{toFaDigits(row.configs)} کانفیگ</span>
                        <span>{toFaDigits(share)}٪ از کل</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            <footer className="modal-foot">
              <button type="button" className="btn ghost" onClick={() => setUsageUser(null)}>
                بستن
              </button>
              <button
                type="button"
                className="btn"
                disabled={usageLoading}
                onClick={() => void openUsage(usageUser)}
              >
                {usageLoading ? "…" : "تازه‌سازی مصرف"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {resetUser && (
        <div
          className="modal-backdrop sheet-backdrop"
          onClick={() => !resetTraffic.isPending && setResetUser(null)}
        >
          <div className="modal sheet-modal" onClick={(e) => e.stopPropagation()}>
            <div className="user-actions-handle mobile-only" aria-hidden />
            <header className="modal-head">
              <div>
                <h3>بازنشانی مصرف</h3>
                <p className="modal-sub">
                  مصرف کاربر <strong dir="ltr">{resetUser.username}</strong> صفر می‌شود
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setResetUser(null)}
                aria-label="بستن"
              >
                <X size={18} />
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              حجم مصرف‌شده در Astra و روی همه پنل‌های متصل صفر می‌شود. سقف خرید و تاریخ انقضا عوض
              نمی‌شود.
              {(resetUser.panel_accounts || []).length > 0 ? (
                <>
                  <br />
                  <span className="muted" style={{ fontSize: "0.9rem" }}>
                    {toFaDigits((resetUser.panel_accounts || []).length)} پنل همگام می‌شود.
                  </span>
                </>
              ) : null}
            </p>
            {resetTraffic.isError && (
              <p className="error">{(resetTraffic.error as Error).message}</p>
            )}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={resetTraffic.isPending}
                onClick={() => setResetUser(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn"
                disabled={resetTraffic.isPending}
                onClick={() => resetTraffic.mutate(resetUser.username)}
              >
                <RotateCcw size={16} />
                {resetTraffic.isPending ? "در حال بازنشانی…" : "بازنشانی مصرف"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {confirmUser && (
        <div className="modal-backdrop" onClick={() => !remove.isPending && setConfirmUser(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>تأیید حذف</h3>
                <p className="modal-sub">
                  کاربر از دیتابیس Astra و از همه پنل‌های ریموت حذف می‌شود
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setConfirmUser(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <p style={{ margin: "0 0 16px", lineHeight: 1.7 }}>
              مطمئنید می‌خواهید کاربر{" "}
              <strong dir="ltr">{confirmUser.username}</strong> را حذف کنید؟
              <br />
              <span className="muted" style={{ fontSize: "0.9rem" }}>
                {(confirmUser.panel_accounts || []).length} پنل درگیر حذف می‌شود. این کار قابل برگشت
                نیست.
              </span>
            </p>
            {remove.isError && <p className="error">{(remove.error as Error).message}</p>}
            <footer className="modal-foot">
              <button
                type="button"
                className="btn ghost"
                disabled={remove.isPending}
                onClick={() => setConfirmUser(null)}
              >
                انصراف
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate(confirmUser.username)}
              >
                {remove.isPending ? "در حال حذف از پنل‌ها…" : "بله، حذف شود"}
              </button>
            </footer>
          </div>
        </div>
      )}

      {errorsUser && (
        <div className="modal-backdrop" onClick={() => setErrorsUser(null)}>
          <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
            <header className="modal-head">
              <div>
                <h3>جزئیات خطا</h3>
                <p className="modal-sub">
                  کاربر <strong dir="ltr">{errorsUser.username}</strong>
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setErrorsUser(null)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>

            {errorAccounts.length === 0 ? (
              <p className="muted">خطایی ثبت نشده</p>
            ) : (
              <div className="user-err-list">
                {errorAccounts.map((a) => (
                  <div key={a.id || a.panel_id} className="user-err-item">
                    <div className="user-err-head">
                      <strong>{a.panel_name || `پنل #${a.panel_id}`}</strong>
                      <span className="badge err">{a.status}</span>
                      {a.panel_type && <span className="badge muted">{a.panel_type}</span>}
                    </div>
                    <pre className="user-err-body" dir="auto">
                      {a.error_message || "بدون پیام خطا"}
                    </pre>
                  </div>
                ))}
              </div>
            )}

            <footer className="modal-foot">
              <button type="button" className="btn ghost" onClick={() => setErrorsUser(null)}>
                بستن
              </button>
              {errorAccounts.some((a) => a.status === "error") && (
                <button
                  type="button"
                  className="btn"
                  disabled={retry.isPending}
                  onClick={() => {
                    retry.mutate(errorsUser.username);
                    setErrorsUser(null);
                  }}
                >
                  تلاش مجدد روی پنل‌های خطا
                </button>
              )}
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
