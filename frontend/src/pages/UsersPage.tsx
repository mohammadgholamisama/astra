import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
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
  GroupCategory,
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
  // Only real error status — never treat leftover error_message on active as error
  return (u.panel_accounts || []).some((a) => a.status === "error");
}

/** Distinct remote panels (not per-group account rows). */
function uniquePanelStats(u: User): { ok: number; total: number; errorPanels: number } {
  const accounts = u.panel_accounts || [];
  const all = new Set<number>();
  const active = new Set<number>();
  const errored = new Set<number>();
  for (const a of accounts) {
    all.add(a.panel_id);
    if (a.status === "active") active.add(a.panel_id);
    if (a.status === "error") errored.add(a.panel_id);
  }
  return { ok: active.size, total: all.size, errorPanels: errored.size };
}

function formatPanelErrorDetail(
  a: User["panel_accounts"][number],
  astraUsername?: string
): string {
  const lines: string[] = [];
  if (a.error_message?.trim()) {
    lines.push(a.error_message.trim());
  } else {
    lines.push("پیام خطایی ثبت نشده");
  }
  if (a.remote_username) {
    const alias =
      astraUsername && a.remote_username !== astraUsername
        ? ` (نام یکتای این پنل برای کاربر Astra «${astraUsername}»)`
        : "";
    lines.push(`یوزرنیم ریموت: ${a.remote_username}${alias}`);
  }
  if (a.panel_base_url) {
    lines.push(`آدرس پنل: ${a.panel_base_url}`);
  }
  const n = a.configs_count ?? (a.links || []).filter(Boolean).length;
  lines.push(`کانفیگ ذخیره‌شده: ${n}`);
  if (a.last_sync_at) {
    lines.push(`آخرین همگام‌سازی: ${a.last_sync_at}`);
  }
  return lines.join("\n");
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

type GroupUsageRow = {
  groupId: number;
  name: string;
  raw: number;
  weighted: number;
  panels: number;
  configs: number;
  /** Distinct coefficients used by this group's panels */
  coefficients: number[];
};

type UsageMode = "panels" | "groups";

const CARD_GROUP_SHOW = 1;

function readCoeff(raw: unknown): number {
  if (typeof raw === "string") {
    const s = raw
      .trim()
      .replace(/[۰-۹]/g, (d) => "۰۱۲۳۴۵۶۷۸۹".indexOf(d).toString())
      .replace(/[٠-٩]/g, (d) => "٠١٢٣٤٥٦٧٨٩".indexOf(d).toString())
      .replace(/,/g, ".");
    const n = Number(s);
    return !Number.isNaN(n) && n >= 0.01 ? n : 1;
  }
  const n = Number(raw ?? 1);
  return !Number.isNaN(n) && n >= 0.01 ? n : 1;
}

function claimCoeff(groups: Group[], groupId: number, panelId: number): number {
  const g = groups.find((x) => x.id === groupId);
  const gp = (g?.panels || []).find((p) => p.panel_id === panelId);
  return readCoeff((gp?.config as { coefficient?: number } | undefined)?.coefficient);
}

/** Coefficient for one PanelAccount: its group×panel config (not max across groups). */
function accountCoeff(a: User["panel_accounts"][number], groups: Group[], groupIds: number[]): number {
  if (a.group_id != null) {
    return claimCoeff(groups, a.group_id, a.panel_id);
  }
  // Legacy merged account: max across user's groups on this panel
  const want = new Set(groupIds || []);
  let best = 1;
  for (const g of groups) {
    if (want.size && !want.has(g.id)) continue;
    const gp = (g.panels || []).find((p) => p.panel_id === a.panel_id);
    if (!gp) continue;
    const c = readCoeff((gp.config as { coefficient?: number } | undefined)?.coefficient);
    if (c > best) best = c;
  }
  return best;
}

function accountWeighted(a: User["panel_accounts"][number], coefficient: number): number {
  const raw = Number(a.used_traffic) || 0;
  const coeff = coefficient >= 0.01 ? coefficient : 1;
  return Math.round(raw * coeff);
}

function formatCoeff(c: number): string {
  return c === Math.floor(c) ? String(c) : c.toFixed(2).replace(/\.?0+$/, "");
}

function panelUsageRows(u: User, groups: Group[]): PanelUsageRow[] {
  const astraLimit = Number(u.data_limit) || 0;
  const usedWeighted = Number(u.used_traffic) || 0;
  const rows: PanelUsageRow[] = [];
  const gids = u.group_ids || [];

  for (const a of u.panel_accounts || []) {
    if (a.status !== "active" && a.status !== "error") continue;
    const raw = Number(a.used_traffic) || 0;
    const coefficient = accountCoeff(a, groups, gids);
    const weighted = accountWeighted(a, coefficient);
    let remoteLimit = 0;
    if (astraLimit > 0) {
      const exhausted = usedWeighted >= astraLimit;
      remoteLimit = exhausted ? Math.max(1, raw || 1) : astraLimit;
    }
    const gName =
      a.group_id != null ? groups.find((g) => g.id === a.group_id)?.name : null;
    rows.push({
      panelId: a.panel_id,
      name: gName
        ? `${a.panel_name || `پنل #${a.panel_id}`} · ${gName}`
        : a.panel_name || `پنل #${a.panel_id}`,
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

/** Per-group: Σ (raw × coeff) only for accounts that belong to that group. */
function groupUsageRows(u: User, groups: Group[]): GroupUsageRow[] {
  const want = new Set(u.group_ids || []);
  const accounts = (u.panel_accounts || []).filter(
    (a) => a.status === "active" || a.status === "error",
  );
  const rows: GroupUsageRow[] = [];
  for (const g of groups) {
    if (want.size && !want.has(g.id)) continue;
    let rawSum = 0;
    let weighted = 0;
    let panels = 0;
    let configs = 0;
    const coeffSet = new Set<number>();
    const mine = accounts.filter((a) => a.group_id === g.id);
    if (mine.length) {
      for (const a of mine) {
        const c = claimCoeff(groups, g.id, a.panel_id);
        const raw = Number(a.used_traffic) || 0;
        rawSum += raw;
        weighted += Math.round(raw * c);
        panels += 1;
        configs += (a.links || []).filter(Boolean).length;
        coeffSet.add(c);
      }
    } else {
      // Legacy: no group_id on accounts — attribute each panel once with this group's coeff
      const seenPanel = new Set<number>();
      for (const gp of g.panels || []) {
        const a = accounts.find(
          (x) => x.panel_id === gp.panel_id && (x.group_id == null || x.group_id === g.id),
        );
        if (!a || seenPanel.has(a.panel_id)) continue;
        seenPanel.add(a.panel_id);
        const c = claimCoeff(groups, g.id, gp.panel_id);
        const raw = Number(a.used_traffic) || 0;
        rawSum += raw;
        weighted += Math.round(raw * c);
        panels += 1;
        configs += (a.links || []).filter(Boolean).length;
        coeffSet.add(c);
      }
    }
    rows.push({
      groupId: g.id,
      name: g.name,
      raw: rawSum,
      weighted,
      panels,
      configs,
      coefficients: [...coeffSet].sort((a, b) => a - b),
    });
  }
  rows.sort((a, b) => b.weighted - a.weighted || a.name.localeCompare(b.name, "fa"));
  return rows;
}

export default function UsersPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [searchParams] = useSearchParams();
  const [search, setSearch] = useState(() => searchParams.get("q")?.trim() || "");
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [filterGroupId, setFilterGroupId] = useState<number | "">("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [dataGb, setDataGb] = useState("50");
  const [days, setDays] = useState("30");
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [groupSearch, setGroupSearch] = useState("");
  /** Filter/focus in create modal: all | uncategorized | category id */
  const [createCatTab, setCreateCatTab] = useState<number | "all" | "none">("all");
  const [editCatTab, setEditCatTab] = useState<number | "all" | "none">("all");
  const [bulkPrefix, setBulkPrefix] = useState("");
  const [bulkCount, setBulkCount] = useState("5");
  const [bulkDisplayName, setBulkDisplayName] = useState("");
  const [bulkDataGb, setBulkDataGb] = useState("50");
  const [bulkDays, setBulkDays] = useState("30");
  const [bulkGroupIds, setBulkGroupIds] = useState<number[]>([]);
  const [bulkGroupSearch, setBulkGroupSearch] = useState("");
  const [bulkCatTab, setBulkCatTab] = useState<number | "all" | "none">("all");
  const [bulkResult, setBulkResult] = useState<{
    created: User[];
    failed: { username: string; message: string }[];
    total_requested: number;
  } | null>(null);
  const [copied, setCopied] = useState("");
  const [confirmUser, setConfirmUser] = useState<User | null>(null);
  const [resetUser, setResetUser] = useState<User | null>(null);
  const [editUser, setEditUser] = useState<User | null>(null);
  const [editDisplayName, setEditDisplayName] = useState("");
  const [editDataGb, setEditDataGb] = useState("50");
  const [editDays, setEditDays] = useState("30");
  /** Full membership while editing — add/remove freely (min 1 on save). */
  const [editGroupIds, setEditGroupIds] = useState<number[]>([]);
  const [editInitialGroupIds, setEditInitialGroupIds] = useState<number[]>([]);
  const [editGroupSearch, setEditGroupSearch] = useState("");
  const [errorsUser, setErrorsUser] = useState<User | null>(null);
  const [subUser, setSubUser] = useState<User | null>(null);
  const [usageUser, setUsageUser] = useState<User | null>(null);
  const [usageMode, setUsageMode] = useState<UsageMode>("panels");
  const [usageLoading, setUsageLoading] = useState(false);
  /** Bumps on open/close so a late API reply cannot reopen the usage modal. */
  const usageFetchId = useRef(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const skipListScroll = useRef(true);
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

  // Dashboard «پرمصرف‌ها» → /users?q=username (also when already on this page)
  useEffect(() => {
    const q = searchParams.get("q")?.trim() || "";
    if (!q) return;
    setSearch(q);
    setSearchDebounced(q);
    setPage(0);
    setFilter("all");
  }, [searchParams]);

  useEffect(() => {
    if (skipListScroll.current) {
      skipListScroll.current = false;
      return;
    }
    const el = listRef.current;
    if (!el) return;
    // Wait a beat so the new page content paints, then ease to the list top
    const t = window.setTimeout(() => {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 40);
    return () => window.clearTimeout(t);
  }, [page]);

  useEffect(() => {
    setPage(0);
  }, [searchDebounced, filterGroupId, filter]);

  type UsersListPayload = {
    users: User[];
    total: number;
    counts?: {
      all: number;
      active: number;
      error: number;
      expired: number;
    };
  };

  const usersQueryKey = [
    "users",
    searchDebounced,
    filterGroupId,
    filter,
    page,
  ] as const;

  function buildUsersQs(opts: { refresh: boolean; pageIndex: number }) {
    const qs = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(opts.pageIndex * PAGE_SIZE),
      refresh: opts.refresh ? "true" : "false",
      filter,
    });
    if (searchDebounced.trim()) qs.set("search", searchDebounced.trim());
    if (filterGroupId !== "") qs.set("group_id", String(filterGroupId));
    return qs;
  }

  const { data, isLoading, isFetching, isSuccess } = useQuery({
    queryKey: usersQueryKey,
    queryFn: () =>
      api<UsersListPayload>(`/api/users?${buildUsersQs({ refresh: false, pageIndex: page })}`),
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnMount: true,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  });

  async function syncUsageNow(silent = false) {
    setSyncingUsage(true);
    try {
      const fresh = await api<UsersListPayload>(
        `/api/users?${buildUsersQs({ refresh: true, pageIndex: page })}`
      );
      qc.setQueryData(usersQueryKey, fresh);
      if (!silent) {
        setActionMsg({ type: "ok", text: "مصرف این صفحه همگام شد" });
      }
    } catch (e) {
      if (!silent) {
        setActionMsg({
          type: "err",
          text: e instanceof Error ? e.message : "همگام‌سازی ناموفق",
        });
      }
    } finally {
      setSyncingUsage(false);
    }
  }

  // After fast paint: pull live usage/configs for this page in parallel (non-blocking)
  useEffect(() => {
    if (!isSuccess || !data?.users?.length) return;
    let cancelled = false;
    const t = window.setTimeout(() => {
      if (cancelled) return;
      void syncUsageNow(true);
    }, 30);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSuccess, searchDebounced, filterGroupId, filter, page]);
  const { data: groups = [] } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api<Group[]>("/api/groups"),
    staleTime: 60_000,
  });
  const { data: groupCategories = [] } = useQuery({
    queryKey: ["group-categories"],
    queryFn: () => api<GroupCategory[]>("/api/group-categories"),
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
          group_ids: groupIds,
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
      setGroupIds([]);
      setGroupSearch("");
      setCreateCatTab("all");
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

  type BulkCreateResponse = {
    created: User[];
    failed: { username: string; message: string }[];
    total_requested: number;
  };

  const bulkCreate = useMutation({
    mutationFn: () => {
      const gb = Number(bulkDataGb);
      const dayN = Number(bulkDays);
      const count = Math.floor(Number(bulkCount) || 0);
      const expire =
        dayN > 0 ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400) : 0;
      const dn = bulkDisplayName.trim();
      return api<BulkCreateResponse>("/api/users/bulk", {
        method: "POST",
        body: JSON.stringify({
          username_prefix: bulkPrefix.trim(),
          count,
          display_name: dn || null,
          data_limit: gb > 0 ? gbToBytes(gb) : 0,
          expire,
          status: "active",
          group_ids: bulkGroupIds,
        }),
      });
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setBulkOpen(false);
      setBulkPrefix("");
      setBulkCount("5");
      setBulkDisplayName("");
      setBulkDataGb("50");
      setBulkDays("30");
      setBulkGroupIds([]);
      setBulkGroupSearch("");
      setBulkCatTab("all");
      setBulkResult(res);
    },
  });

  const bulkErrorText = (() => {
    if (!bulkCreate.isError) return null;
    const err = bulkCreate.error;
    if (err instanceof ApiError) {
      if (err.code === "USERNAME_EXISTS" || err.status === 409) {
        return err.message || "برخی یوزرنیم‌ها از قبل وجود دارند";
      }
      return err.message;
    }
    return err instanceof Error ? err.message : "خطا در ساخت گروهی";
  })();

  const bulkCountN = Math.min(50, Math.max(0, Math.floor(Number(bulkCount) || 0)));
  const bulkPreviewNames = useMemo(() => {
    const p = bulkPrefix.trim();
    if (!p || bulkCountN < 1) return [];
    const n = Math.min(bulkCountN, 5);
    const list = Array.from({ length: n }, (_, i) => `${p}_${i + 1}`);
    if (bulkCountN > 5) list.push(`… ${p}_${bulkCountN}`);
    return list;
  }, [bulkPrefix, bulkCountN]);

  function resetBulkForm() {
    setBulkPrefix("");
    setBulkCount("5");
    setBulkDisplayName("");
    setBulkDataGb("50");
    setBulkDays("30");
    setBulkGroupIds([]);
    setBulkGroupSearch("");
    setBulkCatTab("all");
  }

  function toggleBulkGroup(id: number) {
    setBulkGroupIds((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    );
  }

  function onBulkSubmit(e: FormEvent) {
    e.preventDefault();
    if (bulkGroupIds.length === 0) return;
    if (!bulkPrefix.trim() || bulkCountN < 1) return;
    bulkCreate.mutate();
  }

  function copyAllBulkSubs() {
    const lines = (bulkResult?.created || [])
      .map((u) => `${u.username}\t${u.subscription_url}`)
      .join("\n");
    if (!lines) return;
    copy(lines, "bulk-all");
  }

  function copyAllBulkLinksOnly() {
    const lines = (bulkResult?.created || [])
      .map((u) => u.subscription_url)
      .filter(Boolean)
      .join("\n");
    if (!lines) return;
    copy(lines, "bulk-links");
  }

  const remove = useMutation({
    mutationFn: (u: string) =>
      api<{ detail?: string; skipped_count?: number }>(
        `/api/user/${encodeURIComponent(u)}`,
        { method: "DELETE" }
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setConfirmUser(null);
      const skipped = Number(res?.skipped_count || 0);
      setActionMsg({
        type: "ok",
        text:
          skipped > 0
            ? `کاربر حذف شد — ${toFaDigits(skipped)} پنل خاموش/قطع رد شد`
            : "کاربر از Astra و پنل‌ها حذف شد",
      });
    },
    onError: (e: Error) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setActionMsg({
        type: "err",
        text: e.message || "حذف کاربر ناموفق بود",
      });
    },
  });

  const retry = useMutation({
    mutationFn: (u: string) =>
      api(`/api/user/${encodeURIComponent(u)}/retry`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setErrorsUser(null);
      setActionMsg({
        type: "ok",
        text: "تلاش مجدد انجام شد — پنل‌های خاموش از خطا حذف شدند",
      });
    },
    onError: (e: Error) => setActionMsg({ type: "err", text: e.message }),
  });

  type RetryAllResult = {
    attempted: number;
    recovered: number;
    remaining: number;
    dropped: number;
    skipped_unreachable: number;
    panels_ok: string[];
    panels_down: string[];
    users_recovered: number;
    users_remaining: number;
  };

  const retryAll = useMutation({
    mutationFn: () =>
      api<RetryAllResult>("/api/users/retry-errors", { method: "POST" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setErrorsUser(null);
      let text = "خطای فعالی برای تلاش مجدد نبود";
      if (r.recovered > 0 && r.remaining <= 0) {
        text = `همه خطاها برطرف شد (${toFaDigits(r.recovered)} مورد)`;
      } else if (r.recovered > 0) {
        text = `${toFaDigits(r.recovered)} خطا برطرف شد — ${toFaDigits(r.remaining)} هنوز خطا دارد`;
      } else if (r.dropped > 0 && r.remaining <= 0) {
        text = `خطاهای پنل خاموش پاک شد (${toFaDigits(r.dropped)})`;
      } else if (r.skipped_unreachable > 0 || r.remaining > 0) {
        const names = (r.panels_down || []).slice(0, 3).join("، ");
        text = names
          ? `پنل ${names} هنوز پاسخ نمی‌دهد — خطاها باقی ماند`
          : "پنل‌ها هنوز پاسخ ندادند — خطاها باقی ماند";
      }
      setActionMsg({
        type: r.remaining > 0 && r.recovered <= 0 ? "err" : "ok",
        text,
      });
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

  function patchUserInCache(updated: User) {
    // Instant UI update — don't wait for refetch / full page refresh
    qc.setQueryData(usersQueryKey, (prev: UsersListPayload | undefined) => {
      if (!prev?.users) return prev;
      let hit = false;
      const users = prev.users.map((u) => {
        if (u.username !== updated.username) return u;
        hit = true;
        return { ...u, ...updated };
      });
      return hit ? { ...prev, users } : prev;
    });
    qc.setQueriesData(
      { queryKey: ["users"] },
      (prev: UsersListPayload | undefined) => {
        if (!prev?.users) return prev;
        let hit = false;
        const users = prev.users.map((u) => {
          if (u.username !== updated.username) return u;
          hit = true;
          return { ...u, ...updated };
        });
        return hit ? { ...prev, users } : prev;
      }
    );
  }

  const resetTraffic = useMutation({
    mutationFn: (username: string) =>
      api<User>(`/api/user/${encodeURIComponent(username)}/reset`, {
        method: "POST",
      }),
    onSuccess: (updated) => {
      patchUserInCache(updated);
      setResetUser(null);
      setActionMsg({
        type: "ok",
        text: "مصرف بازنشانی شد",
      });
      // Soft background refresh — UI already shows zeros
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => {
      setResetUser(null);
      setActionMsg({ type: "err", text: e.message });
    },
  });

  const saveEdit = useMutation({
    mutationFn: () => {
      if (!editUser) throw new Error("کاربر انتخاب نشده");
      if (editGroupIds.length === 0) {
        throw new Error("حداقل یک گروه باید انتخاب شود");
      }
      const gb = Number(editDataGb);
      const dayN = Number(editDays);
      const expire =
        dayN > 0 ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400) : 0;
      const dn = editDisplayName.trim();
      const body: Record<string, unknown> = {
        display_name: dn || null,
        clear_display_name: !dn,
        data_limit: gb > 0 ? gbToBytes(gb) : 0,
        expire,
      };
      // Only send group_ids when membership actually changed — avoids a full
      // re-provision of every panel on every volume/expire edit (very slow).
      const groupsChanged =
        editGroupIds.length !== editInitialGroupIds.length ||
        editGroupIds.some((id, i) => id !== editInitialGroupIds[i]);
      if (groupsChanged) {
        body.group_ids = editGroupIds;
      }
      return api<User>(`/api/user/${encodeURIComponent(editUser.username)}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
    },
    onSuccess: (updated) => {
      const initial = new Set(editInitialGroupIds);
      const next = new Set(editGroupIds);
      const added = editGroupIds.filter((id) => !initial.has(id)).length;
      const removed = editInitialGroupIds.filter((id) => !next.has(id)).length;
      const parts: string[] = [];
      if (added) parts.push(`${toFaDigits(added)} گروه اضافه`);
      if (removed) parts.push(`${toFaDigits(removed)} گروه حذف از پنل و ساب`);
      patchUserInCache(updated);
      setEditUser(null);
      setActionMsg({
        type: "ok",
        text: parts.length
          ? `ذخیره شد · ${parts.join(" · ")}`
          : "ذخیره شد",
      });
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e: Error) => setActionMsg({ type: "err", text: e.message }),
  });

  function openEdit(u: User) {
    setActionMsg(null);
    setEditUser(u);
    setEditDisplayName(u.display_name || "");
    setEditDataGb(bytesToGbInput(u.data_limit));
    setEditDays(expireToDaysInput(u.expire));
    const ids = [...(u.group_ids || [])];
    setEditInitialGroupIds(ids);
    setEditGroupIds(ids);
    setEditGroupSearch("");
    setEditCatTab("all");
  }

  function toggleEditGroup(id: number) {
    setEditGroupIds((cur) => {
      if (cur.includes(id)) {
        if (cur.length <= 1) return cur; // keep at least one
        return cur.filter((x) => x !== id);
      }
      return [...cur, id];
    });
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

  function closeUsage() {
    usageFetchId.current += 1;
    setUsageUser(null);
    setUsageLoading(false);
  }

  async function openUsage(u: User, mode: UsageMode = "panels") {
    const fetchId = ++usageFetchId.current;
    setUsageMode(mode);
    setUsageUser(u);
    setUsageLoading(true);
    try {
      const fresh = await api<User>(`/api/user/${encodeURIComponent(u.username)}`);
      if (usageFetchId.current !== fetchId) return;
      setUsageUser(fresh);
      qc.invalidateQueries({ queryKey: ["users"] });
    } catch {
      /* keep list snapshot */
    } finally {
      if (usageFetchId.current === fetchId) setUsageLoading(false);
    }
  }

  const usageRows = useMemo(
    () => (usageUser ? panelUsageRows(usageUser, groups) : []),
    [usageUser, groups]
  );
  const groupUsage = useMemo(
    () => (usageUser ? groupUsageRows(usageUser, groups) : []),
    [usageUser, groups]
  );
  const usageTotalWeighted = useMemo(
    () => usageRows.reduce((s, r) => s + r.weighted, 0),
    [usageRows]
  );
  const groupUsageTotal = useMemo(
    () => groupUsage.reduce((s, r) => s + r.weighted, 0),
    [groupUsage]
  );
  const usageMaxWeighted = useMemo(
    () => Math.max(1, ...usageRows.map((r) => r.weighted), 1),
    [usageRows]
  );
  const groupUsageMax = useMemo(
    () => Math.max(1, ...groupUsage.map((r) => r.weighted), 1),
    [groupUsage]
  );

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (groupIds.length === 0) return;
    create.mutate();
  }

  function toggleCreateGroup(id: number) {
    setGroupIds((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    );
  }

  const users = data?.users || [];
  const totalUsers = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalUsers / PAGE_SIZE));
  const groupsById = useMemo(() => {
    const m = new Map<number, Group>();
    for (const g of groups) m.set(g.id, g);
    return m;
  }, [groups]);

  const activeGroups = useMemo(
    () => groups.filter((g) => !g.is_disabled),
    [groups]
  );

  const uncategorizedActive = useMemo(
    () => activeGroups.filter((g) => g.category_id == null),
    [activeGroups]
  );

  const groupsInCategory = useMemo(() => {
    const map = new Map<number, Group[]>();
    for (const g of activeGroups) {
      const cid = g.category_id;
      if (cid == null) continue;
      const list = map.get(cid) || [];
      list.push(g);
      map.set(cid, list);
    }
    return map;
  }, [activeGroups]);

  function filterGroupsByCatAndSearch(
    catTab: number | "all" | "none",
    searchQ: string
  ): Group[] {
    let list = activeGroups;
    if (catTab === "none") {
      list = uncategorizedActive;
    } else if (catTab !== "all") {
      list = groupsInCategory.get(catTab) || [];
    }
    const q = searchQ.trim().toLowerCase();
    if (!q) return list;
    return list.filter((g) => {
      if (g.name.toLowerCase().includes(q)) return true;
      return (g.panels || []).some(
        (gp) =>
          (gp.panel_name || "").toLowerCase().includes(q) ||
          (gp.panel_type || "").toLowerCase().includes(q)
      );
    });
  }

  const filteredCreateGroups = useMemo(
    () => filterGroupsByCatAndSearch(createCatTab, groupSearch),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeGroups, createCatTab, groupSearch, groupsInCategory, uncategorizedActive]
  );

  const filteredBulkGroups = useMemo(
    () => filterGroupsByCatAndSearch(bulkCatTab, bulkGroupSearch),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeGroups, bulkCatTab, bulkGroupSearch, groupsInCategory, uncategorizedActive]
  );

  const filteredEditGroups = useMemo(() => {
    let list = activeGroups;
    if (editCatTab === "none") {
      list = uncategorizedActive;
    } else if (editCatTab !== "all") {
      list = groupsInCategory.get(editCatTab) || [];
    }
    const q = editGroupSearch.trim().toLowerCase();
    if (!q) return list;
    return list.filter((g) => {
      if (g.name.toLowerCase().includes(q)) return true;
      return (g.panels || []).some(
        (gp) =>
          (gp.panel_name || "").toLowerCase().includes(q) ||
          (gp.panel_type || "").toLowerCase().includes(q)
      );
    });
  }, [
    activeGroups,
    editCatTab,
    editGroupSearch,
    groupsInCategory,
    uncategorizedActive,
  ]);

  function selectCategoryGroups(
    categoryId: number | null,
    mode: "create" | "edit" | "bulk" = "create"
  ) {
    const ids =
      categoryId == null
        ? uncategorizedActive.map((g) => g.id)
        : (groupsInCategory.get(categoryId) || []).map((g) => g.id);
    if (!ids.length) return;
    if (mode === "edit") {
      setEditGroupIds((cur) => [...new Set([...cur, ...ids])]);
      setEditCatTab(categoryId == null ? "none" : categoryId);
      return;
    }
    if (mode === "bulk") {
      setBulkGroupIds((cur) => [...new Set([...cur, ...ids])]);
      setBulkCatTab(categoryId == null ? "none" : categoryId);
      return;
    }
    setGroupIds((cur) => [...new Set([...cur, ...ids])]);
    setCreateCatTab(categoryId == null ? "none" : categoryId);
  }

  const editAddedCount = useMemo(() => {
    const initial = new Set(editInitialGroupIds);
    return editGroupIds.filter((id) => !initial.has(id)).length;
  }, [editGroupIds, editInitialGroupIds]);

  const editRemovedCount = useMemo(() => {
    const next = new Set(editGroupIds);
    return editInitialGroupIds.filter((id) => !next.has(id)).length;
  }, [editGroupIds, editInitialGroupIds]);

  const createPanelSummary = useMemo(() => {
    const panelMap = new Map<
      number,
      { name: string; type: string; coeff: number }
    >();
    for (const gid of groupIds) {
      const g = groupsById.get(gid);
      for (const gp of g?.panels || []) {
        const raw = Number(
          (gp.config as { coefficient?: number } | undefined)?.coefficient ?? 1
        );
        const c = !Number.isNaN(raw) && raw >= 0.01 ? raw : 1;
        const prev = panelMap.get(gp.panel_id);
        if (!prev || c > prev.coeff) {
          panelMap.set(gp.panel_id, {
            name: gp.panel_name || `پنل #${gp.panel_id}`,
            type: gp.panel_type || "",
            coeff: c,
          });
        }
      }
    }
    return [...panelMap.values()];
  }, [groupIds, groupsById]);

  const createGroupHint = useMemo(() => {
    const n = createPanelSummary.length;
    if (!groupIds.length) return "حداقل یک گروه را انتخاب کنید";
    if (!n) return "گروه‌های انتخاب‌شده پنل فعالی ندارند";
    const multi = createPanelSummary.some((p) => p.coeff !== 1);
    if (multi) {
      return `${toFaDigits(n)} پنل یکتا · مصرف = جمع (خام × ضریب) · با اتمام حجم همه خاموش`;
    }
    return `${toFaDigits(n)} پنل یکتا · حجم خرید مشترک بین همه پنل‌ها`;
  }, [groupIds.length, createPanelSummary]);

  function selectAllFilteredGroups() {
    const ids = filteredCreateGroups.map((g) => g.id);
    setGroupIds((cur) => [...new Set([...cur, ...ids])]);
  }

  function clearCreateGroups() {
    setGroupIds([]);
  }

  const panelTypeLabel: Record<string, string> = {
    marzban: "مرزبان",
    pasarguard: "پاسارگاد",
    sanaei: "سنایی",
    astra: "آسترا",
    rebecca: "ربکا",
  };

  const counts = data?.counts;
  const stats = useMemo(() => {
    const now = Math.floor(Date.now() / 1000);
    let expiringSoon = 0;
    let usedSum = 0;
    let limited = 0;
    for (const u of users) {
      usedSum += Number(u.used_traffic) || 0;
      if ((Number(u.data_limit) || 0) > 0) limited += 1;
      const exp = Number(u.expire) || 0;
      if (exp > now && exp - now <= 7 * 86400) expiringSoon += 1;
    }
    return {
      total: counts?.all ?? totalUsers,
      active: counts?.active ?? 0,
      withError: counts?.error ?? 0,
      expired: counts?.expired ?? 0,
      expiringSoon,
      usedSum,
      limited,
      loaded: users.length,
    };
  }, [users, totalUsers, counts]);

  // Server already filters by chip — list is the current page of that filter
  const filtered = users;

  useEffect(() => {
    if (page > 0 && page >= totalPages) setPage(Math.max(0, totalPages - 1));
  }, [page, totalPages]);

  const errorAccounts: PanelAccount[] = useMemo(() => {
    if (!errorsUser) return [];
    return (errorsUser.panel_accounts || []).filter((a) => a.status === "error");
  }, [errorsUser]);

  const filterGroupName =
    filterGroupId === "" ? null : groupsById.get(Number(filterGroupId))?.name || null;

  const filters: { key: FilterKey; label: string; count: number }[] = [
    { key: "all", label: "همه", count: stats.total },
    { key: "active", label: "فعال", count: stats.active },
    { key: "error", label: "خطا", count: stats.withError },
    { key: "expired", label: "منقضی", count: stats.expired },
  ];

  const hasExtraFilter = !!search.trim() || filter !== "all" || filterGroupId !== "";

  return (
    <div className="users-page">
      <header className="users-head">
        <div className="users-head-text">
          <h1 className="page-title">کاربران</h1>
          <p className="page-sub users-head-sub">
            {toFaDigits(stats.total)} کاربر
            {filterGroupName ? ` · ${filterGroupName}` : ""}
            {syncingUsage ? " · همگام‌سازی…" : isFetching ? " · بارگذاری…" : ""}
          </p>
        </div>
        <div className="users-head-actions">
          {stats.withError > 0 && (
            <button
              type="button"
              className="btn ghost sm users-retry-all-btn"
              onClick={() => retryAll.mutate()}
              disabled={retryAll.isPending || retry.isPending}
              title="تلاش مجدد همه خطاهای پنل"
              aria-label="تلاش مجدد همه خطاها"
            >
              <RefreshCw size={15} className={retryAll.isPending ? "spin" : undefined} />
              <span className="users-retry-all-label">
                {retryAll.isPending ? "در حال تلاش…" : "تلاش مجدد همه"}
              </span>
            </button>
          )}
          <button
            type="button"
            className="btn ghost sm users-sync-btn"
            onClick={() => void syncUsageNow()}
            disabled={isFetching || syncingUsage}
            title="تازه‌سازی مصرف از پنل‌ها"
            aria-label="تازه‌سازی مصرف"
          >
            <RefreshCw size={15} className={syncingUsage ? "spin" : undefined} />
            <span className="users-sync-label">تازه‌سازی</span>
          </button>
          <button
            type="button"
            className="btn ghost sm users-bulk-btn"
            onClick={() => {
              resetBulkForm();
              setBulkOpen(true);
            }}
          >
            <Layers size={15} />
            <span className="users-bulk-label">ساخت گروهی</span>
          </button>
          <button
            type="button"
            className="btn users-add"
            onClick={() => {
              setCreateCatTab("all");
              setOpen(true);
            }}
          >
            <Plus size={16} />
            <span>کاربر جدید</span>
          </button>
        </div>
      </header>

      <section className="users-board" aria-label="آمار کاربران">
        <div className="users-stats">
          <article className="users-stat">
            <span className="users-stat-ico" aria-hidden>
              <Users size={15} />
            </span>
            <div className="users-stat-body">
              <strong>{toFaDigits(stats.total)}</strong>
              <span>کل</span>
            </div>
          </article>
          <article className="users-stat ok">
            <span className="users-stat-ico" aria-hidden>
              <Wifi size={15} />
            </span>
            <div className="users-stat-body">
              <strong>{toFaDigits(stats.active)}</strong>
              <span>فعال</span>
            </div>
          </article>
          <article className={`users-stat ${stats.withError ? "danger" : ""}`}>
            <span className="users-stat-ico" aria-hidden>
              <AlertTriangle size={15} />
            </span>
            <div className="users-stat-body">
              <strong>{toFaDigits(stats.withError)}</strong>
              <span>خطا</span>
            </div>
          </article>
          <article className={`users-stat ${stats.expiringSoon ? "warn" : ""}`}>
            <span className="users-stat-ico" aria-hidden>
              <Clock size={15} />
            </span>
            <div className="users-stat-body">
              <strong>{toFaDigits(stats.expiringSoon)}</strong>
              <span>انقضا ۷روز</span>
            </div>
          </article>
          <article className="users-stat wide">
            <span className="users-stat-ico" aria-hidden>
              <BarChart3 size={15} />
            </span>
            <div className="users-stat-body">
              <strong>{formatTraffic(stats.usedSum, { empty: "۰" })}</strong>
              <span>مصرف صفحه</span>
            </div>
          </article>
        </div>
      </section>

      <div className="users-toolbar">
        <div className="users-toolbar-main">
          <div className="users-search">
            <Search size={16} strokeWidth={2} />
            <input
              placeholder="جستجوی یوزرنیم…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              dir="ltr"
              aria-label="جستجوی یوزرنیم"
            />
            {!!search.trim() && (
              <button
                type="button"
                className="users-search-clear"
                onClick={() => setSearch("")}
                aria-label="پاک کردن جستجو"
              >
                <X size={14} />
              </button>
            )}
          </div>
          <div className="users-group-filter">
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
          </div>
        </div>
        <div className="users-filters" role="tablist" aria-label="فیلتر وضعیت">
          {filters.map((f) => (
            <button
              key={f.key}
              type="button"
              role="tab"
              aria-selected={filter === f.key}
              className={`users-filter ${filter === f.key ? "active" : ""}`}
              onClick={() => setFilter(f.key)}
            >
              <span>{f.label}</span>
              <em>{toFaDigits(f.count)}</em>
            </button>
          ))}
          {hasExtraFilter && (
            <button
              type="button"
              className="users-filter users-clear-chip"
              onClick={() => {
                setSearch("");
                setFilter("all");
                setFilterGroupId("");
              }}
            >
              پاک کردن
              <X size={12} />
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

      {stats.withError > 0 && (
        <div className="users-retry-banner">
          <AlertTriangle size={16} />
          <p>
            {toFaDigits(stats.withError)} کاربر با خطای پنل — بعد از وصل شدن پنل، همه را یک‌جا تلاش
            مجدد کنید. خطاهایی که هنوز پنل‌شان پاسخ ندهد باقی می‌مانند.
          </p>
          <button
            type="button"
            className="btn sm"
            onClick={() => retryAll.mutate()}
            disabled={retryAll.isPending || retry.isPending}
          >
            <RefreshCw size={14} className={retryAll.isPending ? "spin" : undefined} />
            {retryAll.isPending ? "در حال تلاش مجدد…" : "تلاش مجدد همه خطاها"}
          </button>
        </div>
      )}

      {isLoading ? (
        <div className="users-skel" aria-hidden>
          <div className="users-skel-card" />
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
            <button
              className="btn"
              onClick={() => {
                setCreateCatTab("all");
                setOpen(true);
              }}
            >
              ساخت اولین کاربر
            </button>
          )}
        </div>
      ) : (
        <>
        <div className="users-list" role="list" ref={listRef}>
          {filtered.map((u) => {
            const errors = (u.panel_accounts || []).filter((a) => a.status === "error");
            const { ok, total: panels, errorPanels } = uniquePanelStats(u);
            const pct = usagePercent(u.used_traffic, u.data_limit);
            const remain = remainingBytes(u.used_traffic, u.data_limit);
            const exp = formatExpire(u.expire);
            const st = statusLabel(u);
            const userGroupList = (u.group_ids || [])
              .map((id) => groupsById.get(id))
              .filter((g): g is Group => !!g);
            const groupTitle = userGroupList.map((g) => g.name).join("، ");
            const shownGroups = userGroupList.slice(0, CARD_GROUP_SHOW);
            const hiddenGroupCount = Math.max(0, userGroupList.length - CARD_GROUP_SHOW);
            const barTone =
              pct == null ? "muted" : pct >= 90 ? "danger" : pct >= 70 ? "warn" : "ok";
            const isOff = u.status === "disabled";
            const menuOpen = menuUser?.username === u.username;
            const initial = (u.username || "?").trim().charAt(0).toUpperCase() || "?";
            return (
              <article
                key={u.username}
                role="listitem"
                className={`user-card tone-${st.tone}${errors.length ? " has-err" : ""}${isOff ? " is-off" : ""}${menuOpen ? " menu-open" : ""}`}
              >
                <span className={`user-card-rail ${st.tone}`} aria-hidden />
                <div className="user-card-top">
                  <div className={`user-card-avatar ${st.tone}`} aria-hidden>
                    {initial}
                  </div>
                  <div className="user-card-who">
                    <div className="user-card-name">
                      <b dir="ltr" title={u.username}>
                        {u.username}
                      </b>
                      <span className={`badge ${st.tone}`}>{st.text}</span>
                    </div>
                    {u.display_name ? (
                      <div className="user-card-display" title={u.display_name}>
                        {u.display_name}
                      </div>
                    ) : null}
                    <div className="user-card-groups" title={groupTitle || undefined}>
                      {userGroupList.length === 0 ? (
                        <span className="user-group-empty">بدون گروه</span>
                      ) : userGroupList.length === 1 ? (
                        <span className="user-group-pill">{userGroupList[0].name}</span>
                      ) : (
                        <>
                          <span className="user-group-count">
                            <Layers size={11} />
                            {toFaDigits(userGroupList.length)} گروه
                          </span>
                          {shownGroups.map((g) => (
                            <span key={g.id} className="user-group-pill soft">
                              {g.name}
                            </span>
                          ))}
                          {hiddenGroupCount > 1 ? (
                            <span className="user-group-more">
                              +{toFaDigits(hiddenGroupCount)}
                            </span>
                          ) : null}
                        </>
                      )}
                    </div>
                  </div>
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

                <div className={`user-card-usage ${barTone}`}>
                  <div className="user-card-usage-line">
                    <span className="user-card-usage-label">مصرف</span>
                    <span className="user-card-usage-vals">
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
                  <div className="user-card-usage-remain">
                    {remain != null
                      ? `باقی‌مانده: ${formatTraffic(remain, { empty: "۰" })}`
                      : "حجم نامحدود"}
                  </div>
                </div>

                <div className="user-card-foot">
                  <span className={`user-card-chip ${exp.tone}`}>
                    <Clock size={12} />
                    {exp.label}
                  </span>
                  <span className="user-card-chip">
                    <Wifi size={12} />
                    {toFaDigits(ok)}/{toFaDigits(panels)} پنل
                  </span>
                  {errorPanels > 0 && (
                    <button
                      type="button"
                      className="user-card-chip err"
                      onClick={() => setErrorsUser(u)}
                    >
                      <AlertTriangle size={12} />
                      {toFaDigits(errorPanels)} خطا
                    </button>
                  )}
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
        const errors = (u.panel_accounts || []).filter((a) => a.status === "error");
        const { ok, total: panels } = uniquePanelStats(u);
        const remain = remainingBytes(u.used_traffic, u.data_limit);
        const exp = formatExpire(u.expire);
        const menuGroups = (u.group_ids || [])
          .map((id) => groupsById.get(id))
          .filter((g): g is Group => !!g);
        const initial = (u.username || "?").trim().charAt(0).toUpperCase() || "?";
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
                <div className={`user-actions-avatar ${st.tone}`} aria-hidden>
                  {initial}
                </div>
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
                    <span>
                      <Layers size={12} />
                      {menuGroups.length
                        ? `${toFaDigits(menuGroups.length)} گروه`
                        : "بدون گروه"}
                    </span>
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
                  {menuGroups.length > 0 ? (
                    <div className="user-actions-groups" title="گروه‌های کاربر">
                      {menuGroups.map((g) => (
                        <span key={g.id} className="user-actions-group-pill">
                          {g.name}
                        </span>
                      ))}
                    </div>
                  ) : null}
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
                    void openUsage(u, "panels");
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
                  onClick={() => {
                    setMenuUser(null);
                    void openUsage(u, "groups");
                  }}
                >
                  <span className="user-actions-ico">
                    <Layers size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>مصرف گروه‌ها</strong>
                    <small>جمع حجم هر گروه با ضریب</small>
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
                    disabled={retry.isPending || retryAll.isPending}
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
                <p className="modal-sub">
                  گروه بزنید تا کانفیگ روی همه پنل‌های همان گروه‌ها ساخته شود
                </p>
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
              <div className="user-create-grid">
                <div className="field">
                  <label>یوزرنیم</label>
                  <input
                    required
                    pattern="[a-zA-Z0-9_]{3,32}"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="user_01"
                    dir="ltr"
                    autoFocus
                  />
                </div>
                <div className="field">
                  <label>نام نمایشی</label>
                  <input
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="اختیاری"
                    maxLength={64}
                  />
                </div>
              </div>

              <div className="field user-create-groups-field">
                <div className="user-create-groups-head">
                  <label>انتخاب گروه</label>
                  <div className="user-create-groups-actions">
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={selectAllFilteredGroups}
                      disabled={!filteredCreateGroups.length}
                    >
                      انتخاب همه
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={clearCreateGroups}
                      disabled={!groupIds.length}
                    >
                      پاک کردن
                    </button>
                  </div>
                </div>

                {(groupCategories.length > 0 || uncategorizedActive.length > 0) && (
                  <div
                    className="user-create-cats"
                    role="tablist"
                    aria-label="دسته‌بندی گروه‌ها"
                  >
                    <button
                      type="button"
                      role="tab"
                      className={`user-create-cat${createCatTab === "all" ? " on" : ""}`}
                      aria-selected={createCatTab === "all"}
                      onClick={() => setCreateCatTab("all")}
                    >
                      همه
                      <em>{toFaDigits(activeGroups.length)}</em>
                    </button>
                    {groupCategories.map((c) => {
                      const n = (groupsInCategory.get(c.id) || []).length;
                      return (
                        <button
                          key={c.id}
                          type="button"
                          role="tab"
                          className={`user-create-cat${createCatTab === c.id ? " on" : ""}`}
                          aria-selected={createCatTab === c.id}
                          disabled={n === 0}
                          title={
                            n === 0
                              ? "گروهی در این دسته نیست"
                              : "انتخاب همه گروه‌های این دسته"
                          }
                          onClick={() => selectCategoryGroups(c.id, "create")}
                        >
                          {c.name}
                          <em>{toFaDigits(n)}</em>
                        </button>
                      );
                    })}
                    {uncategorizedActive.length > 0 && (
                      <button
                        type="button"
                        role="tab"
                        className={`user-create-cat${createCatTab === "none" ? " on" : ""}`}
                        aria-selected={createCatTab === "none"}
                        title="انتخاب گروه‌های بدون دسته"
                        onClick={() => selectCategoryGroups(null, "create")}
                      >
                        بدون دسته
                        <em>{toFaDigits(uncategorizedActive.length)}</em>
                      </button>
                    )}
                  </div>
                )}
                {createCatTab !== "all" && (
                  <p className="field-hint user-create-cat-hint">
                    گروه‌های این دسته به‌صورت پیش‌فرض انتخاب شدند — می‌توانید هر کدام را
                    حذف کنید
                  </p>
                )}

                <div className="user-create-groups-search">
                  <Search size={16} aria-hidden />
                  <input
                    type="search"
                    value={groupSearch}
                    onChange={(e) => setGroupSearch(e.target.value)}
                    placeholder="جستجوی گروه یا پنل…"
                  />
                </div>

                {groupIds.length > 0 && (
                  <div className="user-create-selected" aria-label="گروه‌های انتخاب‌شده">
                    {groupIds.map((id) => {
                      const g = groupsById.get(id);
                      if (!g) return null;
                      return (
                        <button
                          key={id}
                          type="button"
                          className="user-create-chip"
                          onClick={() => toggleCreateGroup(id)}
                          title="کلیک برای حذف"
                        >
                          {g.name}
                          <X size={14} aria-hidden />
                        </button>
                      );
                    })}
                  </div>
                )}

                <div className="user-create-groups" role="group" aria-label="گروه‌ها">
                  {activeGroups.length === 0 ? (
                    <p className="field-hint user-create-empty">ابتدا یک گروه بسازید</p>
                  ) : filteredCreateGroups.length === 0 ? (
                    <p className="field-hint user-create-empty">نتیجه‌ای پیدا نشد</p>
                  ) : (
                    filteredCreateGroups.map((g) => {
                      const on = groupIds.includes(g.id);
                      const ord = on ? groupIds.indexOf(g.id) + 1 : 0;
                      const types = [
                        ...new Set(
                          (g.panels || [])
                            .map((gp) => gp.panel_type || "")
                            .filter(Boolean)
                        ),
                      ];
                      const maxCoeff = Math.max(
                        1,
                        ...(g.panels || []).map((gp) => {
                          const raw = Number(
                            (gp.config as { coefficient?: number } | undefined)
                              ?.coefficient ?? 1
                          );
                          return !Number.isNaN(raw) && raw >= 0.01 ? raw : 1;
                        })
                      );
                      return (
                        <button
                          key={g.id}
                          type="button"
                          className={`user-create-group-card${on ? " on" : ""}`}
                          onClick={() => toggleCreateGroup(g.id)}
                          aria-pressed={on}
                        >
                          <span className={`user-create-check${on ? " on" : ""}`}>
                            {on ? toFaDigits(ord) : null}
                          </span>
                          <span className="user-create-group-body">
                            <span className="user-create-group-name">{g.name}</span>
                            <span className="user-create-group-meta">
                              <span>
                                {toFaDigits(g.panels?.length || 0)} پنل
                              </span>
                              {types.map((t) => (
                                <span key={t} className="user-create-type">
                                  {panelTypeLabel[t] || t}
                                </span>
                              ))}
                              {maxCoeff !== 1 && (
                                <span className="user-create-coeff">
                                  ضریب تا {toFaDigits(maxCoeff)}
                                </span>
                              )}
                            </span>
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>

                <div className="user-create-summary">
                  <p>{createGroupHint}</p>
                  {createPanelSummary.length > 0 && (
                    <p className="user-create-summary-panels">
                      {createPanelSummary
                        .map(
                          (p) =>
                            `${p.name}${p.coeff !== 1 ? ` ×${toFaDigits(p.coeff)}` : ""}`
                        )
                        .join(" · ")}
                    </p>
                  )}
                </div>
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
                    placeholder="50"
                  />
                  <p className="field-hint">سقف مشترک همه پنل‌ها · ۰ = نامحدود</p>
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
                <button
                  className="btn"
                  disabled={create.isPending || groupIds.length === 0}
                >
                  {create.isPending
                    ? "در حال ساخت…"
                    : groupIds.length
                      ? `ساخت روی ${toFaDigits(createPanelSummary.length)} پنل`
                      : "ساخت کاربر"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {bulkOpen && (
        <div
          className="modal-backdrop"
          onClick={() => !bulkCreate.isPending && setBulkOpen(false)}
        >
          <div
            className="modal user-create-modal user-bulk-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="modal-head">
              <div>
                <h3>ساخت گروهی کاربر</h3>
                <p className="modal-sub">
                  چند کاربر با یک پیشوند بسازید —{" "}
                  <span dir="ltr">prefix_1 … prefix_N</span>
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                disabled={bulkCreate.isPending}
                onClick={() => setBulkOpen(false)}
                aria-label="بستن"
              >
                ×
              </button>
            </header>
            <form onSubmit={onBulkSubmit} className="user-create-form">
              <div className="user-create-grid">
                <div className="field">
                  <label>پیشوند یوزرنیم</label>
                  <input
                    required
                    pattern="[a-zA-Z0-9_]{1,28}"
                    value={bulkPrefix}
                    onChange={(e) => setBulkPrefix(e.target.value)}
                    dir="ltr"
                    autoFocus
                    disabled={bulkCreate.isPending}
                  />
                </div>
                <div className="field">
                  <label>تعداد کاربر</label>
                  <input
                    type="number"
                    required
                    min={1}
                    max={50}
                    step={1}
                    inputMode="numeric"
                    value={bulkCount}
                    onChange={(e) => setBulkCount(e.target.value)}
                    dir="ltr"
                    placeholder="5"
                    disabled={bulkCreate.isPending}
                  />
                  <p className="field-hint">حداکثر ۵۰ کاربر در هر بار</p>
                </div>
              </div>

              {bulkPreviewNames.length > 0 && (
                <div className="user-bulk-preview" dir="ltr" aria-live="polite">
                  {bulkPreviewNames.map((n) => (
                    <span key={n} className="user-bulk-preview-chip">
                      {n}
                    </span>
                  ))}
                </div>
              )}

              <div className="field">
                <label>نام نمایشی (اختیاری)</label>
                <input
                  value={bulkDisplayName}
                  onChange={(e) => setBulkDisplayName(e.target.value)}
                  placeholder="برای همه یکسان"
                  maxLength={64}
                  disabled={bulkCreate.isPending}
                />
              </div>

              <div className="field user-create-groups-field">
                <div className="user-create-groups-head">
                  <label>انتخاب گروه</label>
                  <div className="user-create-groups-actions">
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!filteredBulkGroups.length || bulkCreate.isPending}
                      onClick={() =>
                        setBulkGroupIds((cur) => [
                          ...new Set([
                            ...cur,
                            ...filteredBulkGroups.map((g) => g.id),
                          ]),
                        ])
                      }
                    >
                      انتخاب همه
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!bulkGroupIds.length || bulkCreate.isPending}
                      onClick={() => setBulkGroupIds([])}
                    >
                      پاک کردن
                    </button>
                  </div>
                </div>

                {(groupCategories.length > 0 || uncategorizedActive.length > 0) && (
                  <div
                    className="user-create-cats"
                    role="tablist"
                    aria-label="دسته‌بندی گروه‌ها"
                  >
                    <button
                      type="button"
                      role="tab"
                      className={`user-create-cat${bulkCatTab === "all" ? " on" : ""}`}
                      aria-selected={bulkCatTab === "all"}
                      disabled={bulkCreate.isPending}
                      onClick={() => setBulkCatTab("all")}
                    >
                      همه
                      <em>{toFaDigits(activeGroups.length)}</em>
                    </button>
                    {groupCategories.map((c) => {
                      const n = (groupsInCategory.get(c.id) || []).length;
                      return (
                        <button
                          key={c.id}
                          type="button"
                          role="tab"
                          className={`user-create-cat${bulkCatTab === c.id ? " on" : ""}`}
                          aria-selected={bulkCatTab === c.id}
                          disabled={n === 0 || bulkCreate.isPending}
                          onClick={() => selectCategoryGroups(c.id, "bulk")}
                        >
                          {c.name}
                          <em>{toFaDigits(n)}</em>
                        </button>
                      );
                    })}
                    {uncategorizedActive.length > 0 && (
                      <button
                        type="button"
                        role="tab"
                        className={`user-create-cat${bulkCatTab === "none" ? " on" : ""}`}
                        aria-selected={bulkCatTab === "none"}
                        disabled={bulkCreate.isPending}
                        onClick={() => selectCategoryGroups(null, "bulk")}
                      >
                        بدون دسته
                        <em>{toFaDigits(uncategorizedActive.length)}</em>
                      </button>
                    )}
                  </div>
                )}

                <div className="user-create-groups-search">
                  <Search size={16} aria-hidden />
                  <input
                    type="search"
                    value={bulkGroupSearch}
                    onChange={(e) => setBulkGroupSearch(e.target.value)}
                    placeholder="جستجوی گروه…"
                    disabled={bulkCreate.isPending}
                  />
                </div>

                {bulkGroupIds.length > 0 && (
                  <div className="user-create-selected">
                    {bulkGroupIds.map((id) => {
                      const g = groupsById.get(id);
                      if (!g) return null;
                      return (
                        <button
                          key={id}
                          type="button"
                          className="user-create-chip"
                          disabled={bulkCreate.isPending}
                          onClick={() => toggleBulkGroup(id)}
                        >
                          {g.name}
                          <X size={14} aria-hidden />
                        </button>
                      );
                    })}
                  </div>
                )}

                <div className="user-create-groups" role="group">
                  {filteredBulkGroups.length === 0 ? (
                    <p className="field-hint user-create-empty">نتیجه‌ای پیدا نشد</p>
                  ) : (
                    filteredBulkGroups.map((g) => {
                      const on = bulkGroupIds.includes(g.id);
                      return (
                        <button
                          key={g.id}
                          type="button"
                          className={`user-create-group-card${on ? " on" : ""}`}
                          disabled={bulkCreate.isPending}
                          onClick={() => toggleBulkGroup(g.id)}
                          aria-pressed={on}
                        >
                          <span className={`user-create-check${on ? " on" : ""}`}>
                            {on ? <Check size={12} /> : null}
                          </span>
                          <span className="user-create-group-body">
                            <span className="user-create-group-name">{g.name}</span>
                            <span className="user-create-group-meta">
                              {toFaDigits(g.panels?.length || 0)} پنل
                            </span>
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>
              </div>

              <div className="user-create-grid">
                <div className="field">
                  <label>حجم (GB)</label>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    inputMode="decimal"
                    value={bulkDataGb}
                    onChange={(e) => setBulkDataGb(e.target.value)}
                    dir="ltr"
                    disabled={bulkCreate.isPending}
                  />
                </div>
                <div className="field">
                  <label>روز اعتبار</label>
                  <input
                    type="number"
                    min={0}
                    step={1}
                    inputMode="numeric"
                    value={bulkDays}
                    onChange={(e) => setBulkDays(e.target.value)}
                    dir="ltr"
                    disabled={bulkCreate.isPending}
                  />
                </div>
              </div>

              {bulkErrorText && <p className="error">{bulkErrorText}</p>}
              <footer className="modal-foot">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={bulkCreate.isPending}
                  onClick={() => setBulkOpen(false)}
                >
                  انصراف
                </button>
                <button
                  className="btn"
                  disabled={
                    bulkCreate.isPending ||
                    bulkGroupIds.length === 0 ||
                    !bulkPrefix.trim() ||
                    bulkCountN < 1
                  }
                >
                  {bulkCreate.isPending
                    ? `در حال ساخت ${toFaDigits(bulkCountN)} کاربر…`
                    : `ساخت ${toFaDigits(bulkCountN || 0)} کاربر`}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      {bulkResult &&
        createPortal(
          <div
            className="modal-backdrop user-bulk-result-backdrop"
            onClick={() => setBulkResult(null)}
          >
            <div
              className="modal user-bulk-result-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="bulk-result-title"
              onClick={(e) => e.stopPropagation()}
            >
              <header className="modal-head">
                <div>
                  <h3 id="bulk-result-title">کاربران ساخته شدند</h3>
                  <p className="modal-sub">
                    {toFaDigits(bulkResult.created.length)} از{" "}
                    {toFaDigits(bulkResult.total_requested)} موفق
                    {bulkResult.failed.length > 0
                      ? ` · ${toFaDigits(bulkResult.failed.length)} ناموفق`
                      : ""}
                  </p>
                </div>
                <button
                  type="button"
                  className="modal-close"
                  onClick={() => setBulkResult(null)}
                  aria-label="بستن"
                >
                  ×
                </button>
              </header>

              <div className="user-bulk-result-actions">
                <button
                  type="button"
                  className="btn"
                  onClick={copyAllBulkLinksOnly}
                  disabled={!bulkResult.created.length}
                >
                  {copied === "bulk-links" ? (
                    <>
                      <Check size={15} /> کپی شد
                    </>
                  ) : (
                    <>
                      <Copy size={15} /> کپی همه لینک‌ها
                    </>
                  )}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={copyAllBulkSubs}
                  disabled={!bulkResult.created.length}
                >
                  {copied === "bulk-all" ? (
                    <>
                      <Check size={15} /> کپی شد
                    </>
                  ) : (
                    <>
                      <Copy size={15} /> کپی یوزرنیم + لینک
                    </>
                  )}
                </button>
              </div>

              <div className="user-bulk-result-list">
                {bulkResult.created.map((u, i) => (
                  <article key={u.username} className="user-bulk-result-row">
                    <span className="user-bulk-result-rank" aria-hidden>
                      {toFaDigits(i + 1)}
                    </span>
                    <div className="user-bulk-result-meta">
                      <strong dir="ltr">{u.username}</strong>
                      <span className="user-bulk-result-url" dir="ltr" title={u.subscription_url}>
                        {u.subscription_url}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="btn ghost sm"
                      onClick={() => copy(u.subscription_url, `bulk-${u.username}`)}
                      title="کپی لینک ساب"
                    >
                      {copied === `bulk-${u.username}` ? (
                        <Check size={14} />
                      ) : (
                        <Copy size={14} />
                      )}
                    </button>
                  </article>
                ))}
              </div>

              {bulkResult.failed.length > 0 && (
                <div className="user-bulk-result-failed">
                  <p>ناموفق:</p>
                  <ul>
                    {bulkResult.failed.map((f) => (
                      <li key={f.username}>
                        <strong dir="ltr">{f.username}</strong> — {f.message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <footer className="modal-foot">
                <button type="button" className="btn" onClick={() => setBulkResult(null)}>
                  بستن
                </button>
              </footer>
            </div>
          </div>,
          document.body
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

              <div className="field user-create-groups-field">
                <div className="user-create-groups-head">
                  <label>گروه‌های کاربر</label>
                </div>
                <p className="field-hint">
                  گروه اضافه کنید تا کانفیگ روی پنل ساخته و به ساب بیاید. حذف گروه،
                  کانفیگ همان گروه را از پنل اصلی و ساب برمی‌دارد. حداقل یک گروه لازم است.
                  {editAddedCount > 0
                    ? ` · ${toFaDigits(editAddedCount)} اضافه`
                    : ""}
                  {editRemovedCount > 0
                    ? ` · ${toFaDigits(editRemovedCount)} حذف`
                    : ""}
                </p>
                {(groupCategories.length > 0 || uncategorizedActive.length > 0) && (
                  <div
                    className="user-create-cats"
                    role="tablist"
                    aria-label="دسته‌بندی گروه‌ها"
                  >
                    <button
                      type="button"
                      role="tab"
                      className={`user-create-cat${editCatTab === "all" ? " on" : ""}`}
                      aria-selected={editCatTab === "all"}
                      onClick={() => setEditCatTab("all")}
                    >
                      همه
                      <em>{toFaDigits(activeGroups.length)}</em>
                    </button>
                    {groupCategories.map((c) => {
                      const n = (groupsInCategory.get(c.id) || []).length;
                      return (
                        <button
                          key={c.id}
                          type="button"
                          role="tab"
                          className={`user-create-cat${editCatTab === c.id ? " on" : ""}`}
                          aria-selected={editCatTab === c.id}
                          disabled={n === 0}
                          title={
                            n === 0
                              ? "گروهی در این دسته نیست"
                              : "افزودن همه گروه‌های این دسته"
                          }
                          onClick={() => selectCategoryGroups(c.id, "edit")}
                        >
                          {c.name}
                          <em>{toFaDigits(n)}</em>
                        </button>
                      );
                    })}
                    {uncategorizedActive.length > 0 && (
                      <button
                        type="button"
                        role="tab"
                        className={`user-create-cat${editCatTab === "none" ? " on" : ""}`}
                        aria-selected={editCatTab === "none"}
                        title="افزودن گروه‌های بدون دسته"
                        onClick={() => selectCategoryGroups(null, "edit")}
                      >
                        بدون دسته
                        <em>{toFaDigits(uncategorizedActive.length)}</em>
                      </button>
                    )}
                  </div>
                )}
                <div className="user-create-groups-search">
                  <Search size={16} aria-hidden />
                  <input
                    type="search"
                    value={editGroupSearch}
                    onChange={(e) => setEditGroupSearch(e.target.value)}
                    placeholder="جستجوی گروه…"
                  />
                </div>
                {editGroupIds.length > 0 && (
                  <div className="user-create-selected">
                    {editGroupIds.map((id) => {
                      const g = groupsById.get(id);
                      if (!g) return null;
                      const wasInitial = editInitialGroupIds.includes(id);
                      const onlyOne = editGroupIds.length <= 1;
                      return (
                        <button
                          key={id}
                          type="button"
                          className={`user-create-chip${
                            wasInitial ? "" : " is-new"
                          }`}
                          onClick={() => toggleEditGroup(id)}
                          disabled={onlyOne}
                          title={
                            onlyOne
                              ? "حداقل یک گروه باید بماند"
                              : wasInitial
                                ? "حذف گروه — کانفیگ از پنل و ساب حذف می‌شود"
                                : "برداشتن از انتخاب"
                          }
                        >
                          {g.name}
                          {!onlyOne ? <X size={14} aria-hidden /> : null}
                        </button>
                      );
                    })}
                  </div>
                )}
                <div className="user-create-groups" role="group" aria-label="گروه‌های کاربر">
                  {filteredEditGroups.length === 0 ? (
                    <p className="field-hint user-create-empty">نتیجه‌ای پیدا نشد</p>
                  ) : (
                    filteredEditGroups.map((g) => {
                      const on = editGroupIds.includes(g.id);
                      const wasInitial = editInitialGroupIds.includes(g.id);
                      const onlyOne = on && editGroupIds.length <= 1;
                      const types = [
                        ...new Set(
                          (g.panels || [])
                            .map((gp) => gp.panel_type || "")
                            .filter(Boolean)
                        ),
                      ];
                      return (
                        <button
                          key={g.id}
                          type="button"
                          className={`user-create-group-card${on ? " on" : ""}${
                            on && !wasInitial ? " is-new" : ""
                          }`}
                          onClick={() => toggleEditGroup(g.id)}
                          aria-pressed={on}
                          disabled={onlyOne}
                          title={
                            onlyOne
                              ? "حداقل یک گروه باید بماند"
                              : on
                                ? wasInitial
                                  ? "حذف گروه از کاربر"
                                  : "برداشتن"
                                : "افزودن گروه"
                          }
                        >
                          <span className={`user-create-check${on ? " on" : ""}`}>
                            {on ? <Check size={14} strokeWidth={2.5} /> : null}
                          </span>
                          <span className="user-create-group-body">
                            <span className="user-create-group-name">{g.name}</span>
                            <span className="user-create-group-meta">
                              {on && !wasInitial ? "جدید · " : ""}
                              {types
                                .map((t) => panelTypeLabel[t] || t)
                                .join(" · ") || "بدون پنل"}
                            </span>
                          </span>
                        </button>
                      );
                    })
                  )}
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
                <button
                  className="btn"
                  disabled={saveEdit.isPending || editGroupIds.length === 0}
                >
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
        <div className="modal-backdrop sheet-backdrop" onClick={closeUsage}>
          <div
            className="modal modal-lg usage-modal sheet-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="user-actions-handle mobile-only" aria-hidden />
            <header className="modal-head">
              <div>
                <h3>{usageMode === "groups" ? "مصرف گروه‌ها" : "مصرف پنل‌ها"}</h3>
                <p className="modal-sub">
                  <strong dir="ltr">{usageUser.username}</strong>
                  {usageUser.display_name ? ` · ${usageUser.display_name}` : ""}
                  {usageLoading ? " · همگام‌سازی…" : ""}
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={closeUsage}
                aria-label="بستن"
              >
                <X size={18} />
              </button>
            </header>

            <div className="usage-mode-tabs" role="tablist" aria-label="نوع مصرف">
              <button
                type="button"
                role="tab"
                aria-selected={usageMode === "panels"}
                className={usageMode === "panels" ? "active" : ""}
                onClick={() => setUsageMode("panels")}
              >
                پنل‌ها
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={usageMode === "groups"}
                className={usageMode === "groups" ? "active" : ""}
                onClick={() => setUsageMode("groups")}
              >
                گروه‌ها
              </button>
            </div>

            {usageMode === "panels" ? (
              <>
                <p className="usage-modal-hint">
                  مصرف کل = جمع (خام هر پنل × ضریب نهایی). با رسیدن به سقف خرید، وضعیت «اتمام حجم»
                  می‌شود و کانفیگ روی پنل اصلی هم خاموش می‌شود — حتی اگر آنجا حجم مانده باشد.
                </p>

                <div className="usage-modal-total">
                  <span>مصرف کل (با ضریب)</span>
                  <strong>
                    {formatTraffic(usageUser.used_traffic || usageTotalWeighted, {
                      empty: "۰",
                    })}
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
                            <span>ضریب ×{toFaDigits(formatCoeff(row.coefficient))}</span>
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
              </>
            ) : (
              <>
                <p className="usage-modal-hint">
                  مصرف هر گروه = جمع (خام پنل‌های همان گروه × ضریب همان گروه). مصرف کل Astra هر پنل را
                  یک‌بار با ضریب نهایی حساب می‌کند؛ اگر پنلی در چند گروه باشد، در هر گروه جدا دیده
                  می‌شود.
                </p>

                <div className="usage-modal-total">
                  <span>مصرف کل Astra</span>
                  <strong>
                    {formatTraffic(usageUser.used_traffic || usageTotalWeighted, {
                      empty: "۰",
                    })}
                    <small> / {formatDataLimit(usageUser.data_limit)}</small>
                  </strong>
                </div>

                {groupUsage.length === 0 ? (
                  <p className="muted" style={{ textAlign: "center", padding: "20px 0" }}>
                    گروهی برای این کاربر نیست
                  </p>
                ) : (
                  <div className="usage-panel-list">
                    {groupUsage.map((row) => {
                      const width = Math.min(100, (row.weighted / groupUsageMax) * 100);
                      const shareOfAstra =
                        (Number(usageUser.used_traffic) || usageTotalWeighted) > 0
                          ? Math.min(
                              100,
                              Math.round(
                                (row.weighted /
                                  (Number(usageUser.used_traffic) || usageTotalWeighted)) *
                                  100
                              )
                            )
                          : 0;
                      const shareOfGroups =
                        groupUsageTotal > 0
                          ? Math.round((row.weighted / groupUsageTotal) * 100)
                          : 0;
                      const coeffLabel =
                        row.coefficients.length === 0
                          ? "۱"
                          : row.coefficients.length === 1
                            ? formatCoeff(row.coefficients[0])
                            : row.coefficients.map(formatCoeff).join(" / ");
                      return (
                        <div key={row.groupId} className="usage-panel-row">
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
                            <span>{toFaDigits(row.panels)} پنل</span>
                            <span>{toFaDigits(row.configs)} کانفیگ</span>
                            <span>{toFaDigits(shareOfGroups)}٪ از گروه‌ها</span>
                            <span>{toFaDigits(shareOfAstra)}٪ نسبت به کل</span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </>
            )}

            <footer className="modal-foot">
              <button type="button" className="btn ghost" onClick={closeUsage}>
                بستن
              </button>
              <button
                type="button"
                className="btn"
                disabled={usageLoading}
                onClick={() => void openUsage(usageUser, usageMode)}
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
                  کاربر از Astra حذف می‌شود؛ پنل‌های خاموش یا قطع خودکار رد می‌شوند
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
              <p className="muted">خطای واقعی روی هیچ پنلی ثبت نشده</p>
            ) : (
              <div className="user-err-list">
                {errorAccounts.map((a) => (
                  <div key={a.id || a.panel_id} className="user-err-item">
                    <div className="user-err-head">
                      <strong>{a.panel_name || `پنل #${a.panel_id}`}</strong>
                      <span className="badge err">خطا</span>
                      {a.panel_type && <span className="badge muted">{a.panel_type}</span>}
                    </div>
                    <pre className="user-err-body" dir="auto">
                      {formatPanelErrorDetail(a, errorsUser.username)}
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
                  disabled={retry.isPending || retryAll.isPending}
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
