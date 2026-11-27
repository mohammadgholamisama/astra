import {
  CSSProperties,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  Gauge,
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
  Wrench,
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

type FilterKey = "all" | "active" | "error" | "expired" | "limited";

const PAGE_SIZE = 10;

function isCapacityAccountError(msg?: string | null) {
  const m = (msg || "").toLowerCase();
  if (!m) return false;
  if (m.includes("user limit reached") || m.includes("limit reached")) return true;
  if (m.includes("ظرفیت") && (m.includes("پر") || m.includes("گروه"))) return true;
  return m.includes("maximum users") || m.includes("too many users");
}

function isSoftTimeoutAccountError(msg?: string | null) {
  const m = (msg || "").toLowerCase();
  if (!m) return false;
  return (
    m.includes("timeout") ||
    m.includes("timed out") ||
    m.includes("waiting for panel response") ||
    m.includes("طول کشید") ||
    m.includes("تایم")
  );
}

function isDbBusyAccountError(msg?: string | null) {
  const m = (msg || "").toLowerCase();
  if (!m) return false;
  return (
    m.includes("too many clients") ||
    m.includes("remaining connection slots") ||
    m.includes("queuepool") ||
    m.includes("too many open files") ||
    m.includes("connection is closed") ||
    m.includes("pendingrollback")
  );
}

function isOutboundDisabledError(msg?: string | null) {
  const m = msg || "";
  return (
    m.includes("اوتباند") &&
    (m.includes("خاموش") ||
      m.includes("قطع است") ||
      m.includes("تعریف نشده") ||
      m.includes("پینگ ناموفق"))
  );
}

function isNoiseAccountError(msg?: string | null) {
  // Capacity / timeout / outbound-off must stay visible in «خطاها» —
  // admins need to see which panel is full or cut. Only DB storms are noise.
  return isDbBusyAccountError(msg);
}

function isActionableAccountError(
  a: PanelAccount,
  userGroupIds?: number[] | null
) {
  if (a.status !== "error") return false;
  if (a.links && a.links.length > 0) return false;
  if (a.panel_is_active === false) return false;
  if (isNoiseAccountError(a.error_message)) return false;
  // Orphan row for a group the user no longer has — don't surface.
  if (
    a.group_id != null &&
    Array.isArray(userGroupIds) &&
    userGroupIds.length > 0 &&
    !userGroupIds.includes(Number(a.group_id))
  ) {
    return false;
  }
  return true;
}

function userHasErrors(u: User) {
  return (u.panel_accounts || []).some((a) =>
    isActionableAccountError(a, u.group_ids)
  );
}

function accountHasShareLink(a: PanelAccount): boolean {
  const links = a.links || [];
  return links.some((x) => {
    const s = String(x || "").trim().toLowerCase();
    return (
      s.startsWith("vless://") ||
      s.startsWith("vmess://") ||
      s.startsWith("ss://") ||
      s.startsWith("trojan://") ||
      s.startsWith("hysteria") ||
      s.startsWith("tuic://") ||
      s.startsWith("wireguard://")
    );
  });
}

/** Distinct remote panels (not per-group account rows).
 *  ok = has real share link — status=active with empty//sub alone is NOT ok.
 */
function uniquePanelStats(u: User): { ok: number; total: number; errorPanels: number } {
  const accounts = u.panel_accounts || [];
  const all = new Set<number>();
  const okPanels = new Set<number>();
  const errored = new Set<number>();
  for (const a of accounts) {
    if (a.panel_is_active === false && a.status !== "active") continue;
    all.add(a.panel_id);
    if (accountHasShareLink(a)) okPanels.add(a.panel_id);
    if (isActionableAccountError(a, u.group_ids)) {
      errored.add(a.panel_id);
    }
  }
  return { ok: okPanels.size, total: all.size, errorPanels: errored.size };
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

function expireChip(u: User) {
  if (u.status === "on_hold") {
    const dur = Number(u.on_hold_expire_duration) || 0;
    const days = dur > 0 ? Math.max(1, Math.ceil(dur / 86400)) : 0;
    return {
      label: days > 0 ? `از اولین استفاده · ${toFaDigits(days)} روز` : "از اولین استفاده",
      tone: "muted" as const,
    };
  }
  return formatExpire(u.expire);
}

function statusLabel(u: User) {
  if (u.status === "disabled") return { text: "خاموش", tone: "muted" as const };
  if (u.status === "limited") return { text: "اتمام حجم", tone: "warn" as const };
  if (u.status === "expired" || userIsExpired(u)) return { text: "منقضی", tone: "danger" as const };
  if (u.status === "on_hold") return { text: "از اولین استفاده", tone: "muted" as const };
  if (u.status === "active") return { text: "فعال", tone: "ok" as const };
  return { text: u.status || "—", tone: "muted" as const };
}

type PanelUsageRow = {
  key: string;
  panelId: number;
  name: string;
  raw: number;
  coefficient: number;
  weighted: number;
  remoteLimit: number;
  configs: number;
  /** Removed from sub/group but usage still counts toward Astra quota */
  banked: boolean;
  /** Synthetic row: users.used_traffic − Σ visible meters */
  residual?: boolean;
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
  /** Group no longer on user — usage kept from deleted remotes */
  banked: boolean;
  /** Synthetic row: users.used_traffic − Σ group rows */
  residual?: boolean;
};

/** Bytes above this between Astra total and Σ rows count as unexplained gap. */
const USAGE_RESIDUAL_MIN = 1024 * 1024;

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

/** Live group×panel coefficient (what admin configured). */
function liveAccountCoeff(
  a: User["panel_accounts"][number],
  groups: Group[],
  groupIds: number[],
): number {
  if (a.group_id != null) {
    const g = groups.find((x) => x.id === a.group_id);
    const hasLink = (g?.panels || []).some((p) => p.panel_id === a.panel_id);
    if (hasLink) return claimCoeff(groups, a.group_id, a.panel_id);
  }
  const want = new Set(groupIds || []);
  let best = 1;
  for (const g of groups) {
    if (!want.has(g.id)) continue;
    const gp = (g.panels || []).find((p) => p.panel_id === a.panel_id);
    if (!gp) continue;
    const c = readCoeff((gp.config as { coefficient?: number } | undefined)?.coefficient);
    if (c > best) best = c;
  }
  return best;
}

/**
 * Coefficient used for weighted usage — matches backend period lock:
 * prefer applied_coefficient (ingest coeff), else live group×panel config.
 */
function accountCoeff(a: User["panel_accounts"][number], groups: Group[], groupIds: number[]): number {
  const stored = readCoeff(a.applied_coefficient);
  if (a.applied_coefficient != null && stored >= 0.01) return stored;
  if (a.status === "deleted" && stored >= 0.01) return stored;
  const live = liveAccountCoeff(a, groups, groupIds);
  return live >= 0.01 ? live : 1;
}

/** Period-locked weighted bytes — same formula as backend quota_math.weighted_bytes. */
function accountWeighted(
  raw: number,
  coefficient: number,
  account?: User["panel_accounts"][number],
): number {
  const coeff = coefficient >= 0.01 ? coefficient : 1;
  const r = Math.max(0, Math.floor(Number(raw) || 0));
  const ar = Math.max(0, Math.floor(Number(account?.coeff_anchor_raw) || 0));
  const aw = Math.max(0, Math.floor(Number(account?.coeff_anchor_weighted) || 0));
  if (ar <= 0 && aw <= 0) return Math.round(r * coeff);
  return aw + Math.round(Math.max(0, r - ar) * coeff);
}

function formatCoeff(c: number): string {
  return c === Math.floor(c) ? String(c) : c.toFixed(2).replace(/\.?0+$/, "");
}

function remoteKey(panelId: number, name: string | null | undefined): string | null {
  const n = (name || "").trim().toLowerCase();
  return n ? `${panelId}:${n}` : null;
}

/** Live remote identities — deleted twins that share them must not double-count. */
function liveRemoteKeys(accounts: User["panel_accounts"]): Set<string> {
  const keys = new Set<string>();
  for (const a of accounts || []) {
    const counts =
      a.status === "active" ||
      a.status === "error" ||
      (a.status === "pending" && (Number(a.used_traffic) || 0) > 0);
    if (!counts) continue;
    for (const raw of [a.remote_username]) {
      const k = remoteKey(a.panel_id, raw);
      if (k) keys.add(k);
    }
  }
  return keys;
}

function liveRawForDeleted(
  account: User["panel_accounts"][number],
  accounts: User["panel_accounts"],
): number {
  const mine = new Set<string>();
  const k = remoteKey(account.panel_id, account.remote_username);
  if (k) mine.add(k);
  if (!mine.size) return 0;
  let best = 0;
  for (const other of accounts || []) {
    if (other.status !== "active" && other.status !== "error") continue;
    if (other.panel_id !== account.panel_id) continue;
    const ok = remoteKey(other.panel_id, other.remote_username);
    if (ok && mine.has(ok)) {
      best = Math.max(best, Number(other.used_traffic) || 0);
    }
  }
  return best;
}

function panelUsageRows(u: User, groups: Group[]): PanelUsageRow[] {
  const astraLimit = Number(u.data_limit) || 0;
  const usedWeighted = Number(u.used_traffic) || 0;
  const rows: PanelUsageRow[] = [];
  const gids = u.group_ids || [];
  const accounts = u.panel_accounts || [];
  const liveKeys = liveRemoteKeys(accounts);
  // Dedupe identical remotes across groups — same rule as backend weighted Σ.
  const bestByRemote = new Map<string, PanelUsageRow>();

  for (const a of accounts) {
    const banked = a.status === "deleted";
    const pendingMeter =
      a.status === "pending" && (Number(a.used_traffic) || 0) > 0;
    if (!banked && a.status !== "active" && a.status !== "error" && !pendingMeter) {
      continue;
    }

    let raw = Number(a.used_traffic) || 0;
    if (banked) {
      if (raw <= 0) continue;
      const rk = remoteKey(a.panel_id, a.remote_username);
      if (rk && liveKeys.has(rk)) {
        const gap = raw - liveRawForDeleted(a, accounts);
        if (gap <= 0) continue;
        raw = gap;
      }
    }

    const coefficient = accountCoeff(a, groups, gids);
    const weighted = accountWeighted(raw, coefficient, a);
    let remoteLimit = 0;
    if (astraLimit > 0) {
      const exhausted = usedWeighted >= astraLimit;
      if (exhausted) {
        remoteLimit = Math.max(1, raw || 1);
      } else {
        const remain = Math.max(0, astraLimit - usedWeighted);
        const grow = coefficient > 0 ? Math.round(remain / coefficient) : remain;
        remoteLimit = raw + Math.max(0, grow);
      }
    }
    const gName =
      a.group_id != null ? groups.find((g) => g.id === a.group_id)?.name : null;
    const baseName = gName
      ? `${a.panel_name || `پنل #${a.panel_id}`} · ${gName}`
      : a.panel_name || `پنل #${a.panel_id}`;
    const row: PanelUsageRow = {
      key: `${a.id}-${a.panel_id}-${a.group_id ?? "x"}-${banked ? "b" : "l"}`,
      panelId: a.panel_id,
      name: banked ? `${baseName} · ریمو شده` : baseName,
      raw,
      coefficient,
      weighted,
      remoteLimit,
      configs: banked ? 0 : (a.links || []).filter(Boolean).length,
      banked,
    };
    const rk = remoteKey(a.panel_id, a.remote_username);
    if (rk && !banked) {
      const prev = bestByRemote.get(rk);
      if (!prev || row.weighted > prev.weighted || (row.weighted === prev.weighted && row.coefficient > prev.coefficient)) {
        bestByRemote.set(rk, row);
      }
    } else {
      rows.push(row);
    }
  }
  for (const row of bestByRemote.values()) rows.push(row);
  rows.sort((a, b) => b.weighted - a.weighted || Number(a.banked) - Number(b.banked));
  return rows;
}

/** Per-group: Σ (raw × coeff) for live + banked accounts of that group.

  Only:
  - groups the user currently belongs to, and/or
  - groups that still have banked (deleted) meters for this user.

  Never paint unrelated Astra groups when membership is empty — that was the
  «اسم گروه‌های دیگه میاد» bug after removing the only group.
 */
function groupUsageRows(u: User, groups: Group[]): GroupUsageRow[] {
  const want = new Set(u.group_ids || []);
  const accounts = (u.panel_accounts || []).filter(
    (a) =>
      a.status === "active" ||
      a.status === "error" ||
      a.status === "deleted" ||
      (a.status === "pending" && (Number(a.used_traffic) || 0) > 0),
  );
  const liveKeys = liveRemoteKeys(u.panel_accounts || []);
  const byGroup = new Map<number, User["panel_accounts"]>();
  for (const a of accounts) {
    if (a.group_id == null) continue;
    const list = byGroup.get(a.group_id) || [];
    list.push(a);
    byGroup.set(a.group_id, list);
  }

  const rows: GroupUsageRow[] = [];
  const seenGroup = new Set<number>();

  const pushGroup = (g: Group, bankedOnly: boolean) => {
    seenGroup.add(g.id);
    let rawSum = 0;
    let weighted = 0;
    let panels = 0;
    let configs = 0;
    const coeffSet = new Set<number>();
    // Prefer accounts tagged with this group_id. Legacy null group_id only
    // when the user is still a member of THIS group (never for strangers).
    const mine = accounts.filter((a) => a.group_id === g.id);
    if (mine.length) {
      for (const a of mine) {
        let raw = Number(a.used_traffic) || 0;
        if (a.status === "deleted") {
          if (raw <= 0) continue;
          const rk = remoteKey(a.panel_id, a.remote_username);
          if (rk && liveKeys.has(rk)) {
            const gap = raw - liveRawForDeleted(a, u.panel_accounts || []);
            if (gap <= 0) continue;
            raw = gap;
          }
        }
        const c = accountCoeff(a, groups, u.group_ids || []);
        rawSum += raw;
        weighted += accountWeighted(raw, c, a);
        panels += 1;
        configs += a.status === "deleted" ? 0 : (a.links || []).filter(Boolean).length;
        coeffSet.add(c);
      }
    } else if (!bankedOnly && want.has(g.id)) {
      // Legacy: null group_id accounts — only for current memberships.
      const seenPanel = new Set<number>();
      for (const gp of g.panels || []) {
        const a = accounts.find(
          (x) =>
            x.panel_id === gp.panel_id &&
            x.status !== "deleted" &&
            x.group_id == null,
        );
        if (!a || seenPanel.has(a.panel_id)) continue;
        seenPanel.add(a.panel_id);
        const c = accountCoeff(a, groups, u.group_ids || []);
        const raw = Number(a.used_traffic) || 0;
        rawSum += raw;
        weighted += accountWeighted(raw, c, a);
        panels += 1;
        configs += (a.links || []).filter(Boolean).length;
        coeffSet.add(c);
      }
    }
    // Hide empty stranger/banked rows; keep empty current memberships only
    // when the user is still in the group (shows «۰» until first traffic).
    if (rawSum <= 0 && weighted <= 0 && panels <= 0) {
      if (bankedOnly || !want.has(g.id)) return;
    }
    rows.push({
      groupId: g.id,
      name: bankedOnly ? `${g.name} · ریمو شده` : g.name,
      raw: rawSum,
      weighted,
      panels,
      configs,
      coefficients: [...coeffSet].sort((a, b) => a - b),
      banked: bankedOnly,
    });
  };

  // Current memberships only — never «all groups» when want is empty.
  for (const g of groups) {
    if (!want.has(g.id)) continue;
    pushGroup(g, false);
  }
  // Groups removed from the user but still holding banked deleted meters
  for (const [gid, list] of byGroup) {
    if (seenGroup.has(gid)) continue;
    if (!list.some((a) => a.status === "deleted" && (Number(a.used_traffic) || 0) > 0)) {
      continue;
    }
    const g = groups.find((x) => x.id === gid);
    if (g) {
      pushGroup(g, true);
    } else {
      // Group row deleted from Astra entirely — still show a synthetic label
      let rawSum = 0;
      let weighted = 0;
      let panels = 0;
      const coeffSet = new Set<number>();
      for (const a of list) {
        if (a.status !== "deleted") continue;
        let raw = Number(a.used_traffic) || 0;
        if (raw <= 0) continue;
        const rk = remoteKey(a.panel_id, a.remote_username);
        if (rk && liveKeys.has(rk)) {
          const gap = raw - liveRawForDeleted(a, u.panel_accounts || []);
          if (gap <= 0) continue;
          raw = gap;
        }
        const c = accountCoeff(a, groups, u.group_ids || []);
        rawSum += raw;
        weighted += Math.round(raw * c);
        panels += 1;
        coeffSet.add(c);
      }
      if (rawSum > 0 || weighted > 0) {
        rows.push({
          groupId: gid,
          name: `گروه #${gid} · ریمو شده`,
          raw: rawSum,
          weighted,
          panels,
          configs: 0,
          coefficients: [...coeffSet].sort((a, b) => a - b),
          banked: true,
        });
      }
    }
  }
  rows.sort((a, b) => b.weighted - a.weighted || a.name.localeCompare(b.name, "fa"));
  return rows;
}

export default function UsersPage() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [search, setSearch] = useState(() => searchParams.get("q")?.trim() || "");
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [filterGroupId, setFilterGroupId] = useState<number | "">("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [dataGb, setDataGb] = useState("50");
  const [days, setDays] = useState("30");
  const [startOnFirstUse, setStartOnFirstUse] = useState(false);
  const [includePromoFirst, setIncludePromoFirst] = useState(true);
  const [groupIds, setGroupIds] = useState<number[]>([]);
  const [groupSearch, setGroupSearch] = useState("");
  /** Filter/focus in create modal: null=unset | all | uncategorized | category id */
  const [createCatTab, setCreateCatTab] = useState<
    number | "all" | "none" | null
  >(null);
  /** When set, user tracks this category (subs sync on category changes) */
  const [createBoundCategoryId, setCreateBoundCategoryId] = useState<number | null>(
    null
  );
  const [editCatTab, setEditCatTab] = useState<number | "all" | "none">("all");
  const [editBoundCategoryId, setEditBoundCategoryId] = useState<number | null>(
    null
  );
  const [bulkPrefix, setBulkPrefix] = useState("");
  const [bulkCount, setBulkCount] = useState("5");
  const [bulkDisplayName, setBulkDisplayName] = useState("");
  const [bulkDataGb, setBulkDataGb] = useState("50");
  const [bulkDays, setBulkDays] = useState("30");
  const [bulkStartOnFirstUse, setBulkStartOnFirstUse] = useState(false);
  const [bulkIncludePromoFirst, setBulkIncludePromoFirst] = useState(true);
  const [bulkGroupIds, setBulkGroupIds] = useState<number[]>([]);
  const [bulkGroupSearch, setBulkGroupSearch] = useState("");
  const [bulkCatTab, setBulkCatTab] = useState<
    number | "all" | "none" | null
  >(null);
  const [bulkBoundCategoryId, setBulkBoundCategoryId] = useState<number | null>(
    null
  );
  const [createResult, setCreateResult] = useState<User | null>(null);
  const [bulkResult, setBulkResult] = useState<{
    created: User[];
    failed: { username: string; message: string }[];
    total_requested: number;
    remote_bg?: boolean;
    message?: string;
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
    const t = window.setTimeout(() => setSearchDebounced(search), 180);
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

  type UsersListCounts = {
    all: number;
    active: number;
    error: number;
    expired: number;
    limited: number;
  };

  type UsersListPayload = {
    users: User[];
    total: number;
    counts?: UsersListCounts;
  };

  const usersQueryKey = [
    "users",
    searchDebounced,
    filterGroupId,
    filter,
    page,
  ] as const;

  const countsQueryKey = [
    "users",
    "counts",
    searchDebounced,
    filterGroupId,
  ] as const;

  function buildUsersQs(opts: {
    refresh: boolean;
    pageIndex: number;
    details?: boolean;
  }) {
    const qs = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(opts.pageIndex * PAGE_SIZE),
      refresh: opts.refresh ? "true" : "false",
      filter,
      include_counts: "false",
      details: opts.details === false ? "false" : "true",
    });
    if (searchDebounced.trim()) qs.set("search", searchDebounced.trim());
    if (filterGroupId !== "") qs.set("group_id", String(filterGroupId));
    return qs;
  }

  // Phase 1 — lean rows first so the board paints before chip/error SQL.
  const { data, isLoading, isFetching, isSuccess } = useQuery({
    queryKey: usersQueryKey,
    queryFn: () =>
      api<UsersListPayload>(
        `/api/users?${buildUsersQs({
          refresh: false,
          pageIndex: page,
          details: false,
        })}`,
        { timeoutMs: 20_000 }
      ),
    staleTime: 25_000,
    refetchInterval: 45_000,
    refetchOnMount: true,
    refetchOnWindowFocus: false,
    // While typing a search, don't keep the previous board page as placeholder
    // (it looked like the list "vanished" mid-type). Page flips can reuse.
    placeholderData: searchDebounced.trim()
      ? undefined
      : (prev) => prev,
  });

  // Phase 2 — panel accounts for cards (merge into the same cache).
  // Must also run on search — otherwise chips stay ۰/N while remotes are already active.
  const { isFetching: detailsFetching } = useQuery({
    queryKey: [...usersQueryKey, "details"],
    enabled: isSuccess && (data?.users?.length ?? 0) > 0,
    queryFn: async () => {
      const full = await api<UsersListPayload>(
        `/api/users?${buildUsersQs({
          refresh: false,
          pageIndex: page,
          details: true,
        })}`,
        { timeoutMs: 35_000 }
      );
      qc.setQueryData(
        usersQueryKey,
        (prev: UsersListPayload | undefined) => {
          if (!prev) return full;
          const map = new Map(full.users.map((u) => [u.username, u]));
          return {
            ...prev,
            total: full.total || prev.total,
            users: prev.users.map((u) => map.get(u.username) || u),
          };
        }
      );
      return true;
    },
    staleTime: 25_000,
    refetchOnWindowFocus: false,
  });

  // Phase 3 — chip totals (all/active/error/expired/limited) after list is visible.
  const {
    data: chipCounts,
    isFetching: countsFetching,
    isPending: countsPending,
  } = useQuery({
    queryKey: countsQueryKey,
    queryFn: () => {
      const qs = new URLSearchParams();
      if (searchDebounced.trim()) qs.set("search", searchDebounced.trim());
      if (filterGroupId !== "") qs.set("group_id", String(filterGroupId));
      const q = qs.toString();
      return api<UsersListCounts>(
        `/api/users/counts${q ? `?${q}` : ""}`,
        { timeoutMs: 30_000 }
      );
    },
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  });

  // Prefetch neighbors so prev/next feel instant
  useEffect(() => {
    if (!isSuccess) return;
    const neighbors = [page - 1, page + 1].filter((p) => p >= 0);
    for (const p of neighbors) {
      void qc.prefetchQuery({
        queryKey: ["users", searchDebounced, filterGroupId, filter, p],
        queryFn: () =>
          api<UsersListPayload>(
            `/api/users?${buildUsersQs({
              refresh: false,
              pageIndex: p,
              details: false,
            })}`,
            { timeoutMs: 20_000 }
          ),
        staleTime: 12_000,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, isSuccess, searchDebounced, filterGroupId, filter]);

  async function syncUsageNow(silent = false) {
    setSyncingUsage(true);
    try {
      const fresh = await api<UsersListPayload>(
        `/api/users?${buildUsersQs({
          refresh: true,
          pageIndex: page,
          details: true,
        })}`,
        { timeoutMs: 60_000 }
      );
      qc.setQueryData(usersQueryKey, fresh);
      void qc.invalidateQueries({ queryKey: countsQueryKey });
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

  // Live usage stays accurate via fleet/hot/quota loops.
  // Do NOT auto-call refresh=true — that stampeded remotes + DB pool and froze the panel.
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
    mutationFn: async () => {
      const gb = Number(dataGb);
      const dayN = Number(days);
      const hold = startOnFirstUse && dayN > 0;
      const expire = hold
        ? 0
        : dayN > 0
          ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400)
          : 0;
      const dn = displayName.trim();
      const uname = username.trim();
      const recoverExisting = async (): Promise<User | null> => {
        for (let i = 0; i < 3; i++) {
          if (i > 0) {
            await new Promise((r) => window.setTimeout(r, 200 * i));
          }
          try {
            const existing = await api<User>(
              `/api/user/${encodeURIComponent(uname)}`,
              { timeoutMs: 6_000 }
            );
            if (existing?.username) return existing;
          } catch {
            /* retry */
          }
        }
        return null;
      };
      try {
        return await api<User>("/api/user", {
          method: "POST",
          // Local commit + sub URL only; panels finish in background.
          timeoutMs: 8_000,
          body: JSON.stringify({
            username: uname,
            display_name: dn || null,
            data_limit: gb > 0 ? gbToBytes(gb) : 0,
            expire,
            status: hold ? "on_hold" : "active",
            on_hold_expire_duration: hold ? Math.floor(dayN * 86400) : 0,
            include_promo_first: includePromoFirst,
            group_ids: groupIds,
            category_id: createBoundCategoryId,
          }),
        });
      } catch (err) {
        // Timeout / 5xx after commit → user is often already in Astra.
        const soft =
          err instanceof ApiError &&
          (err.status === 409 ||
            err.code === "USERNAME_EXISTS" ||
            err.status === 502 ||
            err.status === 503 ||
            err.status === 504 ||
            err.status >= 500 ||
            /تایم‌اوت|نرسید/.test(err.message));
        if (soft) {
          const existing = await recoverExisting();
          if (existing) return existing;
        }
        throw err;
      }
    },
    onSuccess: (user) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setOpen(false);
      setUsername("");
      setDisplayName("");
      setDataGb("50");
      setDays("30");
      setStartOnFirstUse(false);
      setIncludePromoFirst(true);
      setGroupIds([]);
      setGroupSearch("");
      setCreateCatTab(null);
      setCreateBoundCategoryId(null);
      setCreateResult(user);
      setCopied("");
      setActionMsg({
        type: "ok",
        text: "کاربر و لینک ساب آماده‌اند — ساخت پنل‌ها در پس‌زمینه",
      });
    },
  });

  const createErrorText = (() => {
    if (!create.isError) return null;
    const err = create.error;
    if (err instanceof ApiError) {
      if (err.code === "USERNAME_EXISTS" || err.status === 409) {
        return "این یوزرنیم قبلاً ثبت شده است — یوزرنیم دیگری انتخاب کنید";
      }
      if (err.code === "HOLD_DURATION_REQUIRED") {
        return "برای شروع از اولین استفاده باید روز اعتبار بیشتر از صفر باشد";
      }
      if (err.code === "CREATE_READBACK") {
        return err.message;
      }
      return err.message;
    }
    return err instanceof Error ? err.message : "خطا در ساخت کاربر";
  })();

  type BulkCreateResponse = {
    created: User[];
    failed: { username: string; message: string }[];
    total_requested: number;
    remote_bg?: boolean;
    message?: string;
  };

  const bulkCreate = useMutation({
    mutationFn: () => {
      const gb = Number(bulkDataGb);
      const dayN = Number(bulkDays);
      const count = Math.floor(Number(bulkCount) || 0);
      const hold = bulkStartOnFirstUse && dayN > 0;
      const expire = hold
        ? 0
        : dayN > 0
          ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400)
          : 0;
      const dn = bulkDisplayName.trim();
      // Local users + sub URLs return fast; panel remotes continue in bg.
      return api<BulkCreateResponse>("/api/users/bulk", {
        method: "POST",
        timeoutMs: 60_000,
        body: JSON.stringify({
          username_prefix: bulkPrefix.trim(),
          count,
          display_name: dn || null,
          data_limit: gb > 0 ? gbToBytes(gb) : 0,
          expire,
          status: hold ? "on_hold" : "active",
          on_hold_expire_duration: hold ? Math.floor(dayN * 86400) : 0,
          include_promo_first: bulkIncludePromoFirst,
          group_ids: bulkGroupIds,
          category_id: bulkBoundCategoryId,
        }),
      });
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      // Remotes finish in bg — refresh meters so cards leave ۰/۰ / pending ASAP.
      if (res.remote_bg) {
        for (const ms of [2500, 8000, 20000]) {
          window.setTimeout(() => {
            void qc.invalidateQueries({ queryKey: ["users"] });
          }, ms);
        }
      }
      setBulkOpen(false);
      setBulkPrefix("");
      setBulkCount("5");
      setBulkDisplayName("");
      setBulkDataGb("50");
      setBulkDays("30");
      setBulkStartOnFirstUse(false);
      setBulkIncludePromoFirst(true);
      setBulkGroupIds([]);
      setBulkGroupSearch("");
      setBulkCatTab("all");
      setBulkBoundCategoryId(null);
      setBulkResult(res);
      setCopied("");
    },
  });

  const bulkErrorText = (() => {
    if (!bulkCreate.isError) return null;
    const err = bulkCreate.error;
    if (err instanceof ApiError) {
      if (err.code === "USERNAME_EXISTS" || err.status === 409) {
        return err.message || "برخی یوزرنیم‌ها از قبل وجود دارند";
      }
      if (err.code === "HOLD_DURATION_REQUIRED") {
        return "برای شروع از اولین استفاده باید روز اعتبار بیشتر از صفر باشد";
      }
      if (err.status === 504) {
        return "ساخت طولانی شد — اگر کاربران در لیست آمدند، لینک ساب‌ها آماده‌اند و ساخت پنل در پس‌زمینه ادامه دارد";
      }
      return err.message;
    }
    return err instanceof Error ? err.message : "خطا در ساخت گروهی";
  })();

  const bulkCountN = Math.min(10000, Math.max(0, Math.floor(Number(bulkCount) || 0)));
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
    setBulkStartOnFirstUse(false);
    setBulkGroupIds([]);
    setBulkGroupSearch("");
    setBulkCatTab(null);
    setBulkBoundCategoryId(null);
  }

  function toggleBulkGroup(id: number) {
    setBulkGroupIds((cur) => {
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      setBulkBoundCategoryId((bind) => keepBindIfInsideCategory(next, bind));
      return next;
    });
  }

  function onBulkSubmit(e: FormEvent) {
    e.preventDefault();
    if (bulkGroupIds.length === 0) return;
    if (!bulkPrefix.trim() || bulkCountN < 1) return;
    if (bulkStartOnFirstUse && Number(bulkDays) <= 0) return;
    bulkCreate.mutate();
  }

  function bulkAllLinksText() {
    return (bulkResult?.created || [])
      .map((u) => u.subscription_url)
      .filter(Boolean)
      .join("\n");
  }

  function copyAllBulkSubs() {
    const lines = (bulkResult?.created || [])
      .map((u) => `${u.username}\t${u.subscription_url}`)
      .join("\n");
    if (!lines) return;
    copy(lines, "bulk-all");
  }

  function copyAllBulkLinksOnly() {
    const lines = bulkAllLinksText();
    if (!lines) return;
    const el = document.getElementById(
      "bulk-all-links-box"
    ) as HTMLTextAreaElement | null;
    if (el) {
      el.focus();
      el.select();
    }
    copy(lines, "bulk-links");
  }

  const remove = useMutation({
    mutationFn: (u: string) =>
      api<{ detail?: string; skipped_count?: number; panels_count?: number; remote_bg?: boolean }>(
        `/api/user/${encodeURIComponent(u)}`,
        { method: "DELETE", timeoutMs: 30000 }
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setConfirmUser(null);
      const n = Number(res?.panels_count || 0);
      setActionMsg({
        type: "ok",
        text:
          res?.remote_bg && n > 0
            ? `کاربر از Astra حذف شد — حذف از ${toFaDigits(n)} پنل در پس‌زمینه`
            : "کاربر حذف شد",
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

  type FixSubBlocker = {
    panel_id: number;
    panel_name?: string | null;
    kind: string;
    reason: string;
  };

  type FixSubResult = {
    username: string;
    before_share: number;
    after_share: number;
    before_gaps: number;
    after_gaps: number;
    retried: number;
    healed: number;
    hydrated: number;
    message: string;
    blockers?: FixSubBlocker[];
    user?: User | null;
  };

  const fixSub = useMutation({
    mutationFn: (u: string) =>
      api<FixSubResult>(`/api/user/${encodeURIComponent(u)}/fix-sub`, {
        method: "POST",
        timeoutMs: 120_000,
      }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      const stuck = (r.blockers && r.blockers.length > 0) || r.after_gaps > 0;
      setActionMsg({
        type: stuck ? "err" : "ok",
        text: r.message || "تکمیل کانفیگ‌ها انجام شد",
      });
      // Open errors modal with stamped panel reasons (capacity / outbound off).
      if (stuck && r.user) {
        setErrorsUser(r.user);
      } else {
        setErrorsUser(null);
      }
    },
    onError: (e: Error) =>
      setActionMsg({ type: "err", text: e.message || "تکمیل کانفیگ‌ها ناموفق بود" }),
  });

  type CheckUsageResult = {
    username: string;
    used_before: number;
    used_after: number;
    data_limit: number;
    status: string;
    accounts: number;
    synced: number;
    dt_ms: number;
    message: string;
    panels: Array<{
      panel_id: number;
      panel_name?: string | null;
      panel_type?: string | null;
      remote_username?: string | null;
      status: string;
      used_raw: number;
      last_sync_at?: string | null;
    }>;
    user?: User | null;
  };

  const checkUsage = useMutation({
    mutationFn: (u: string) =>
      api<CheckUsageResult>(
        `/api/user/${encodeURIComponent(u)}/check-usage`,
        { method: "POST", timeoutMs: 100_000 }
      ),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      const before = formatTraffic(r.used_before, { empty: "۰" });
      const after = formatTraffic(r.used_after, { empty: "۰" });
      const delta = r.used_after - r.used_before;
      const deltaLabel =
        delta > 0
          ? ` (+${formatTraffic(delta, { empty: "۰" })})`
          : delta < 0
            ? ` (${formatTraffic(delta, { empty: "۰" })})`
            : "";
      setActionMsg({
        type: "ok",
        text:
          `چک مصرف ${r.username}: ${before} → ${after}${deltaLabel} · ` +
          `${toFaDigits(r.synced)}/${toFaDigits(r.accounts)} پنل · ` +
          `${toFaDigits(Math.max(1, Math.round(r.dt_ms / 1000)))}ث`,
      });
    },
    onError: (e: Error) =>
      setActionMsg({
        type: "err",
        text: e.message || "چک مصرف از پنل‌های اصلی ناموفق بود",
      }),
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
    remote_bg?: boolean;
    message?: string;
  };

  const retryAll = useMutation({
    mutationFn: () =>
      api<RetryAllResult>("/api/users/retry-errors", {
        method: "POST",
        // Instant sweep + bg waves — never wait on thousands of remotes.
        timeoutMs: 45_000,
      }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["users"] });
      qc.invalidateQueries({ queryKey: ["users", "counts"] });
      setErrorsUser(null);
      let text = r.message || "خطای فعالی برای تلاش مجدد نبود";
      if (r.remote_bg) {
        text =
          r.message ||
          (r.remaining > 0
            ? `پاک‌سازی انجام شد — رفع ${toFaDigits(r.remaining)} مورد در پس‌زمینه ادامه دارد`
            : "پاک‌سازی انجام شد");
      } else if (r.recovered > 0 && r.remaining <= 0) {
        text = `همه خطاها برطرف شد (${toFaDigits(r.recovered)} مورد)`;
      } else if (r.recovered > 0) {
        text = `${toFaDigits(r.recovered)} مورد درست شد — ${toFaDigits(r.remaining)} خطای واقعی باقی ماند`;
      } else if (r.dropped > 0 && r.remaining <= 0) {
        text = `خطاهای پنل خاموش پاک شد (${toFaDigits(r.dropped)})`;
      } else if (r.remaining > 0) {
        const names = (r.panels_down || []).slice(0, 3).join("، ");
        text = names
          ? `${toFaDigits(r.remaining)} خطا باقی ماند (پنل قطع: ${names})`
          : `${toFaDigits(r.remaining)} خطای واقعی پنل باقی ماند`;
      } else if ((r.panels_down || []).length > 0) {
        const names = (r.panels_down || []).slice(0, 3).join("، ");
        text = `پنل قطع است: ${names}`;
      }
      setActionMsg({
        type: "ok",
        text,
      });
      // Refresh board after bg waves start recovering.
      window.setTimeout(() => {
        void qc.invalidateQueries({ queryKey: ["users"] });
        void qc.invalidateQueries({ queryKey: ["users", "counts"] });
      }, 12_000);
    },
    onError: (e: Error) => {
      const msg = e.message || "تلاش مجدد ناموفق بود";
      setActionMsg({
        type: "err",
        text: /failed to fetch|networkerror|load failed/i.test(msg)
          ? "ارتباط قطع شد — یک‌بار دیگر بزنید؛ رفع خطا در پس‌زمینه هم ادامه دارد"
          : msg,
      });
    },
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
        // Local zero returns fast; remotes continue in background on the server.
        timeoutMs: 25_000,
      }),
    onSuccess: (updated) => {
      patchUserInCache(updated);
      setResetUser(null);
      setActionMsg({
        type: "ok",
        text: "مصرف صفر شد — ریست پنل‌های اصلی در پس‌زمینه ادامه دارد",
      });
      // Soft background refresh — UI already shows zeros
      void qc.invalidateQueries({ queryKey: ["users"] });
      window.setTimeout(() => {
        void qc.invalidateQueries({ queryKey: ["users"] });
      }, 8_000);
    },
    onError: (e: Error) => {
      setResetUser(null);
      setActionMsg({ type: "err", text: e.message });
    },
  });

  const saveEdit = useMutation({
    mutationFn: async () => {
      if (!editUser) throw new Error("کاربر انتخاب نشده");
      if (editGroupIds.length === 0) {
        throw new Error("حداقل یک گروه باید انتخاب شود");
      }
      const uname = editUser.username;
      const gb = Number(editDataGb);
      const dayN = Number(editDays);
      const stillHold = editUser.status === "on_hold";
      const expire = stillHold
        ? 0
        : dayN > 0
          ? Math.floor(Date.now() / 1000) + Math.floor(dayN * 86400)
          : 0;
      const dn = editDisplayName.trim();
      const body: Record<string, unknown> = {
        display_name: dn || null,
        clear_display_name: !dn,
        data_limit: gb > 0 ? gbToBytes(gb) : 0,
        expire,
      };
      if (stillHold) {
        body.status = "on_hold";
        body.on_hold_expire_duration = dayN > 0 ? Math.floor(dayN * 86400) : 0;
      }
      // Only send group_ids when membership actually changed — avoids a full
      // re-provision of every panel on every volume/expire edit (very slow).
      const groupsChanged =
        editGroupIds.length !== editInitialGroupIds.length ||
        editGroupIds.some((id, i) => id !== editInitialGroupIds[i]);
      if (groupsChanged) {
        body.group_ids = editGroupIds;
      }
      const prevCat = editUser.source_category_id ?? null;
      if (editBoundCategoryId != null) {
        body.category_id = editBoundCategoryId;
      } else if (prevCat != null && groupsChanged) {
        body.clear_category = true;
      }
      try {
        return await api<User>(`/api/user/${encodeURIComponent(uname)}`, {
          method: "PUT",
          timeoutMs: 20_000,
          body: JSON.stringify(body),
        });
      } catch (err) {
        const soft =
          err instanceof ApiError &&
          (err.status === 502 ||
            err.status === 503 ||
            err.status === 504 ||
            err.status >= 500 ||
            /تایم‌اوت|نرسید/.test(err.message));
        if (soft) {
          for (let i = 0; i < 5; i++) {
            if (i > 0) {
              await new Promise((r) => window.setTimeout(r, 300 * i));
            }
            try {
              const existing = await api<User>(
                `/api/user/${encodeURIComponent(uname)}`,
                { timeoutMs: 12_000 }
              );
              if (existing?.username) return existing;
            } catch {
              /* retry */
            }
          }
        }
        throw err;
      }
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
      setEditBoundCategoryId(null);
      setActionMsg({
        type: "ok",
        text: parts.length
          ? `ذخیره شد · ${parts.join(" · ")} · همگام‌سازی پنل‌ها در پس‌زمینه`
          : "ذخیره شد · همگام‌سازی پنل‌ها در پس‌زمینه",
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
    if (u.status === "on_hold" && Number(u.on_hold_expire_duration) > 0) {
      setEditDays(
        String(Math.max(1, Math.ceil(Number(u.on_hold_expire_duration) / 86400)))
      );
    } else {
      setEditDays(expireToDaysInput(u.expire));
    }
    const ids = [...(u.group_ids || [])];
    setEditInitialGroupIds(ids);
    setEditGroupIds(ids);
    setEditGroupSearch("");
    setEditCatTab("all");
    setEditBoundCategoryId(u.source_category_id ?? null);
  }

  function toggleEditGroup(id: number) {
    setEditGroupIds((cur) => {
      if (cur.includes(id) && cur.length <= 1) return cur;
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      setEditBoundCategoryId((bind) => keepBindIfInsideCategory(next, bind));
      return next;
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

  const usageRows = useMemo(() => {
    if (!usageUser) return [] as PanelUsageRow[];
    const rows = panelUsageRows(usageUser, groups);
    const astra = Number(usageUser.used_traffic) || 0;
    const sum = rows.reduce((s, r) => s + r.weighted, 0);
    const gap = astra - sum;
    if (gap >= USAGE_RESIDUAL_MIN) {
      rows.push({
        key: "residual-astra-gap",
        panelId: -1,
        name: "اختلاف متر (بدون ردیف پنل)",
        raw: gap,
        coefficient: 1,
        weighted: gap,
        remoteLimit: 0,
        configs: 0,
        banked: true,
        residual: true,
      });
    }
    return rows;
  }, [usageUser, groups]);
  const groupUsage = useMemo(() => {
    if (!usageUser) return [] as GroupUsageRow[];
    const rows = groupUsageRows(usageUser, groups);
    const astra = Number(usageUser.used_traffic) || 0;
    const sum = rows.reduce((s, r) => s + r.weighted, 0);
    const gap = astra - sum;
    if (gap >= USAGE_RESIDUAL_MIN) {
      rows.push({
        groupId: -1,
        name: "اختلاف متر (بدون ردیف گروه)",
        raw: gap,
        weighted: gap,
        panels: 0,
        configs: 0,
        coefficients: [1],
        banked: true,
        residual: true,
      });
    }
    return rows;
  }, [usageUser, groups]);
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
    if (startOnFirstUse && Number(days) <= 0) return;
    create.mutate();
  }

  function keepBindIfInsideCategory(
    nextIds: number[],
    currentBind: number | null
  ): number | null {
    if (currentBind == null) return null;
    const catIds = new Set(
      (groupsInCategory.get(currentBind) || []).map((g) => g.id)
    );
    if (nextIds.some((id) => !catIds.has(id))) return null;
    return currentBind;
  }

  function toggleCreateGroup(id: number) {
    setGroupIds((cur) => {
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      setCreateBoundCategoryId((bind) => keepBindIfInsideCategory(next, bind));
      return next;
    });
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
    catTab: number | "all" | "none" | null,
    searchQ: string
  ): Group[] {
    let list = activeGroups;
    if (catTab === "none") {
      list = uncategorizedActive;
    } else if (catTab != null && catTab !== "all") {
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
    // Named category → bind user to auto-sync; «بدون دسته» is a one-shot pick
    const bindId = categoryId != null ? categoryId : null;
    if (mode === "edit") {
      setEditGroupIds(ids);
      setEditCatTab(categoryId == null ? "none" : categoryId);
      setEditBoundCategoryId(bindId);
      return;
    }
    if (mode === "bulk") {
      setBulkGroupIds(ids);
      setBulkCatTab(categoryId == null ? "none" : categoryId);
      setBulkBoundCategoryId(bindId);
      return;
    }
    setGroupIds(ids);
    setCreateCatTab(categoryId == null ? "none" : categoryId);
    setCreateBoundCategoryId(bindId);
  }

  /** Tab «همه»: select every active group so create/bulk runs on all of them. */
  function selectAllGroups(mode: "create" | "bulk" = "create") {
    const ids = activeGroups.map((g) => g.id);
    if (!ids.length) return;
    if (mode === "bulk") {
      setBulkGroupIds(ids);
      setBulkCatTab("all");
      setBulkBoundCategoryId(null);
      return;
    }
    setGroupIds(ids);
    setCreateCatTab("all");
    setCreateBoundCategoryId(null);
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
    setGroupIds((cur) => {
      const next = [...new Set([...cur, ...ids])];
      if (typeof createCatTab === "number") {
        setCreateBoundCategoryId(
          keepBindIfInsideCategory(next, createCatTab) ?? createBoundCategoryId
        );
      } else {
        setCreateBoundCategoryId((bind) => keepBindIfInsideCategory(next, bind));
      }
      return next;
    });
  }

  function clearCreateGroups() {
    setGroupIds([]);
    setCreateBoundCategoryId(null);
  }

  const panelTypeLabel: Record<string, string> = {
    marzban: "مرزبان",
    pasarguard: "پاسارگاد",
    sanaei: "سنایی",
    astra: "آسترا",
    rebecca: "ربکا",
    guard: "گارد",
  };

  const counts = chipCounts ?? data?.counts;
  const countsReady = Boolean(chipCounts);
  const stats = useMemo(() => {
    const now = Math.floor(Date.now() / 1000);
    let expiringSoon = 0;
    let usedSum = 0;
    for (const u of users) {
      usedSum += Number(u.used_traffic) || 0;
      const exp = Number(u.expire) || 0;
      if (exp > now && exp - now <= 7 * 86400) expiringSoon += 1;
    }
    return {
      total: counts?.all ?? totalUsers,
      active: counts?.active ?? 0,
      withError: counts?.error ?? 0,
      expired: counts?.expired ?? 0,
      limited: counts?.limited ?? 0,
      expiringSoon,
      usedSum,
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
    // Only actionable faults — inactive panels / orphan groups / pool noise stay hidden
    return (errorsUser.panel_accounts || []).filter((a) =>
      isActionableAccountError(a, errorsUser.group_ids)
    );
  }, [errorsUser]);

  const filterGroupName =
    filterGroupId === "" ? null : groupsById.get(Number(filterGroupId))?.name || null;

  const filters: {
    key: FilterKey;
    label: string;
    count: number | null;
  }[] = [
    { key: "all", label: "همه", count: countsReady ? stats.total : null },
    { key: "active", label: "فعال", count: countsReady ? stats.active : null },
    {
      key: "limited",
      label: "اتمام حجم",
      count: countsReady ? stats.limited : null,
    },
    { key: "error", label: "خطا", count: countsReady ? stats.withError : null },
    {
      key: "expired",
      label: "منقضی",
      count: countsReady ? stats.expired : null,
    },
  ];

  const hasExtraFilter = !!search.trim() || filter !== "all" || filterGroupId !== "";

  return (
    <div className="users-page">
      <header className="users-head">
        <div className="users-head-text">
          <h1 className="page-title">کاربران</h1>
          <p className="page-sub users-head-sub">
            {countsReady ? toFaDigits(stats.total) : "…"} کاربر
            {filterGroupName ? ` · ${filterGroupName}` : ""}
            {syncingUsage
              ? " · همگام‌سازی…"
              : detailsFetching
                ? " · جزئیات پنل…"
                : countsFetching || countsPending
                  ? " · آمار…"
                  : isFetching
                    ? " · بارگذاری…"
                    : ""}
          </p>
        </div>
        <div className="users-head-actions">
          {countsReady && stats.withError > 0 && (
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
              setGroupIds([]);
              setGroupSearch("");
              setCreateCatTab(null);
              setCreateBoundCategoryId(null);
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
              <strong className={!countsReady ? "users-stat-pending" : undefined}>
                {countsReady ? toFaDigits(stats.total) : "…"}
              </strong>
              <span>کل</span>
            </div>
          </article>
          <article className="users-stat ok">
            <span className="users-stat-ico" aria-hidden>
              <Wifi size={15} />
            </span>
            <div className="users-stat-body">
              <strong className={!countsReady ? "users-stat-pending" : undefined}>
                {countsReady ? toFaDigits(stats.active) : "…"}
              </strong>
              <span>فعال</span>
            </div>
          </article>
          <article className={`users-stat ${stats.withError ? "danger" : ""}`}>
            <span className="users-stat-ico" aria-hidden>
              <AlertTriangle size={15} />
            </span>
            <div className="users-stat-body">
              <strong className={!countsReady ? "users-stat-pending" : undefined}>
                {countsReady ? toFaDigits(stats.withError) : "…"}
              </strong>
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
              <em className={f.count == null ? "users-filter-pending" : undefined}>
                {f.count == null ? "…" : toFaDigits(f.count)}
              </em>
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

      {countsReady && stats.withError > 0 && (
        <div className="users-retry-banner">
          <AlertTriangle size={16} />
          <p>
            {toFaDigits(stats.withError)} کاربر فعال با خطای واقعی پنل — سیستم در پس‌زمینه
            درست می‌کند؛ برای تسریع همین حالا هم می‌توانید بزنید.
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
                setGroupIds([]);
                setGroupSearch("");
                setCreateCatTab(null);
                setCreateBoundCategoryId(null);
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
            const errors = (u.panel_accounts || []).filter((a) =>
              isActionableAccountError(a, u.group_ids)
            );
            const { ok, total: panels, errorPanels } = uniquePanelStats(u);
            const pct = usagePercent(u.used_traffic, u.data_limit);
            const remain = remainingBytes(u.used_traffic, u.data_limit);
            const exp = expireChip(u);
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
                  <div
                    className={`user-bar ${barTone}${pct != null && pct > 0 ? " has-use" : ""}`}
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct == null ? undefined : pct}
                    title={pct == null ? undefined : `${pct}%`}
                    style={
                      {
                        ["--bar-pct"]:
                          pct == null
                            ? "0%"
                            : `${Math.min(100, Math.max(0, pct))}%`,
                      } as CSSProperties
                    }
                  />
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
            <div className="users-page-nums" role="list">
              {(() => {
                // Show ~5 numbered pages around current, plus first/last shortcuts
                const last = Math.max(0, totalPages - 1);
                const windowSize = Math.min(5, last + 1);
                let start = Math.max(0, page - Math.floor(windowSize / 2));
                let end = start + windowSize - 1;
                if (end > last) {
                  end = last;
                  start = Math.max(0, end - windowSize + 1);
                }
                const pages: number[] = [];
                for (let i = start; i <= end; i += 1) pages.push(i);
                return (
                  <>
                    {start > 0 && (
                      <>
                        <button
                          type="button"
                          className="users-page-num"
                          onClick={() => setPage(0)}
                          aria-label="صفحه ۱"
                        >
                          {toFaDigits(1)}
                        </button>
                        {start > 1 && (
                          <span className="users-page-ellipsis" aria-hidden>
                            …
                          </span>
                        )}
                      </>
                    )}
                    {pages.map((i) => (
                      <button
                        key={i}
                        type="button"
                        role="listitem"
                        className={`users-page-num${i === page ? " is-active" : ""}`}
                        aria-current={i === page ? "page" : undefined}
                        aria-label={`صفحه ${toFaDigits(i + 1)}`}
                        onClick={() => setPage(i)}
                      >
                        {toFaDigits(i + 1)}
                      </button>
                    ))}
                    {end < last && (
                      <>
                        {end < last - 1 && (
                          <span className="users-page-ellipsis" aria-hidden>
                            …
                          </span>
                        )}
                        <button
                          type="button"
                          className="users-page-num"
                          onClick={() => setPage(last)}
                          aria-label={`صفحه ${toFaDigits(last + 1)}`}
                        >
                          {toFaDigits(last + 1)}
                        </button>
                      </>
                    )}
                  </>
                );
              })()}
            </div>
            <div className="users-pagination-bar">
              <button
                type="button"
                className="users-page-btn"
                disabled={page <= 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                aria-label="صفحه قبلی"
              >
                <ChevronRight size={15} strokeWidth={2.25} aria-hidden />
                <span>قبلی</span>
              </button>
              <span className="users-pagination-range">
                صفحه {toFaDigits(page + 1)} از {toFaDigits(totalPages)}
                <em>
                  · {toFaDigits(page * PAGE_SIZE + 1)}–
                  {toFaDigits(Math.min((page + 1) * PAGE_SIZE, totalUsers))} از{" "}
                  {toFaDigits(totalUsers)}
                  {isFetching ? " · …" : ""}
                </em>
              </span>
              <button
                type="button"
                className="users-page-btn"
                disabled={page + 1 >= totalPages}
                onClick={() => setPage((p) => p + 1)}
                aria-label="صفحه بعدی"
              >
                <span>بعدی</span>
                <ChevronLeft size={15} strokeWidth={2.25} aria-hidden />
              </button>
            </div>
          </nav>
        )}
        </>
      )}

      {menuUser && (() => {
        const u = menuUser;
        const st = statusLabel(u);
        const isOff = u.status === "disabled";
        const errors = (u.panel_accounts || []).filter((a) =>
          isActionableAccountError(a, u.group_ids)
        );
        const { ok, total: panels } = uniquePanelStats(u);
        const remain = remainingBytes(u.used_traffic, u.data_limit);
        const exp = expireChip(u);
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
                  disabled={checkUsage.isPending}
                  onClick={() => {
                    setMenuUser(null);
                    setActionMsg({
                      type: "ok",
                      text: `در حال چک مصرف دقیق ${u.username} از پنل‌های اصلی…`,
                    });
                    checkUsage.mutate(u.username);
                  }}
                >
                  <span className="user-actions-ico">
                    <Gauge
                      size={18}
                      className={checkUsage.isPending ? "spin" : undefined}
                    />
                  </span>
                  <span className="user-actions-label">
                    <strong>
                      {checkUsage.isPending
                        ? "در حال چک مصرف…"
                        : "چک مصرف دقیق"}
                    </strong>
                    <small>استعلام زنده از همه پنل‌های اصلی</small>
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
                  onClick={() => {
                    setMenuUser(null);
                    navigate(`/analytics?u=${encodeURIComponent(u.username)}`);
                  }}
                >
                  <span className="user-actions-ico">
                    <Activity size={18} />
                  </span>
                  <span className="user-actions-label">
                    <strong>تحلیل رفتار</strong>
                    <small>سرعت، نشست، دستگاه و اینترنت ساب</small>
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
                <button
                  type="button"
                  role="menuitem"
                  disabled={fixSub.isPending || retry.isPending}
                  onClick={() => {
                    setMenuUser(null);
                    setActionMsg({
                      type: "ok",
                      text: `در حال تکمیل کانفیگ‌های ${u.username}…`,
                    });
                    fixSub.mutate(u.username);
                  }}
                >
                  <span className="user-actions-ico">
                    <Wrench size={18} className={fixSub.isPending ? "spin" : undefined} />
                  </span>
                  <span className="user-actions-label">
                    <strong>
                      {fixSub.isPending ? "در حال تکمیل…" : "تکمیل کانفیگ‌ها"}
                    </strong>
                    <small>رفع اختلال ساب و پر کردن لیست ناقص</small>
                  </span>
                </button>
                {errors.length > 0 && (
                  <button
                    type="button"
                    role="menuitem"
                    disabled={retry.isPending || retryAll.isPending || fixSub.isPending}
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
                      title="انتخاب همه گروه‌ها"
                      onClick={() => selectAllGroups("create")}
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
                {createBoundCategoryId != null ? (
                  <p className="field-hint user-create-cat-hint">
                    متصل به دسته — همه گروه‌های این دسته تیک خوردند. می‌توانید یکی را
                    بردارید. بعداً با افزودن یا حذف گروه در همین دسته، کانفیگ برای این
                    کاربر هم ساخته یا از پنل و ساب حذف می‌شود.
                  </p>
                ) : createCatTab === "all" ? (
                  <p className="field-hint user-create-cat-hint">
                    همه گروه‌ها انتخاب شدند — کاربر روی همه ساخته می‌شود. می‌توانید یکی
                    را بردارید.
                  </p>
                ) : (
                  <p className="field-hint user-create-cat-hint">
                    روی «همه» بزنید تا همه گروه‌ها انتخاب شوند، یا روی نام دسته تا فقط
                    گروه‌های همان دسته انتخاب و وصل شوند.
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
                  <p className="field-hint">
                    {startOnFirstUse ? "از لحظهٔ اولین اتصال شمرده می‌شود" : "۰ = نامحدود"}
                  </p>
                </div>
              </div>
              <button
                type="button"
                className={`settings-switch user-hold-switch${startOnFirstUse ? " on" : ""}`}
                role="switch"
                aria-checked={startOnFirstUse}
                onClick={() => setStartOnFirstUse((v) => !v)}
              >
                <span className="settings-switch-track" aria-hidden>
                  <span className="settings-switch-knob" />
                </span>
                <span className="user-hold-switch-copy">
                  <strong>شروع از اولین استفاده</strong>
                  <small>
                    {startOnFirstUse
                      ? "تایمر روز اعتبار بعد از اولین اتصال روی همه پنل‌ها با هم شروع می‌شود"
                      : "خاموش: تایمر از همین الان شروع می‌شود"}
                  </small>
                </span>
              </button>
              {startOnFirstUse && Number(days) <= 0 && (
                <p className="error">برای این گزینه باید روز اعتبار بیشتر از صفر باشد</p>
              )}
              <button
                type="button"
                className={`settings-switch user-hold-switch${includePromoFirst ? " on" : ""}`}
                role="switch"
                aria-checked={includePromoFirst}
                onClick={() => setIncludePromoFirst((v) => !v)}
              >
                <span className="settings-switch-track" aria-hidden>
                  <span className="settings-switch-knob" />
                </span>
                <span className="user-hold-switch-copy">
                  <strong>تبلیغ اول</strong>
                  <small>
                    {includePromoFirst
                      ? "کانفیگ تبلیغ اول تنظیمات در ساب این کاربر نمایش داده می‌شود"
                      : "تبلیغ اول برای این کاربر در ساب نمی‌آید"}
                  </small>
                </span>
              </button>
              {createErrorText && <p className="error">{createErrorText}</p>}
              <footer className="modal-foot">
                <button type="button" className="btn ghost" onClick={() => setOpen(false)}>
                  انصراف
                </button>
                <button
                  className="btn"
                  disabled={
                    create.isPending ||
                    groupIds.length === 0 ||
                    (startOnFirstUse && Number(days) <= 0)
                  }
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
                    max={10000}
                    step={1}
                    inputMode="numeric"
                    value={bulkCount}
                    onChange={(e) => setBulkCount(e.target.value)}
                    dir="ltr"
                    placeholder="5"
                    disabled={bulkCreate.isPending}
                  />
                  <p className="field-hint">
                    بدون سقف ۵۰ — هر تعداد کاربر؛ ساخت پنل‌ها و کانفیگ در پس‌زمینه با سرعت بالا ادامه می‌یابد
                  </p>
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
                        setBulkGroupIds((cur) => {
                          const next = [
                            ...new Set([
                              ...cur,
                              ...filteredBulkGroups.map((g) => g.id),
                            ]),
                          ];
                          if (typeof bulkCatTab === "number") {
                            setBulkBoundCategoryId(
                              keepBindIfInsideCategory(next, bulkCatTab) ??
                                bulkBoundCategoryId
                            );
                          } else {
                            setBulkBoundCategoryId((bind) =>
                              keepBindIfInsideCategory(next, bind)
                            );
                          }
                          return next;
                        })
                      }
                    >
                      انتخاب همه
                    </button>
                    <button
                      type="button"
                      className="btn ghost sm"
                      disabled={!bulkGroupIds.length || bulkCreate.isPending}
                      onClick={() => {
                        setBulkGroupIds([]);
                        setBulkBoundCategoryId(null);
                      }}
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
                      title="انتخاب همه گروه‌ها"
                      onClick={() => selectAllGroups("bulk")}
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
                {bulkBoundCategoryId != null ? (
                  <p className="field-hint user-create-cat-hint">
                    متصل به دسته — همه گروه‌ها تیک خوردند. می‌توانید یکی را بردارید.
                    بعداً با اد/ریمو گروه در این دسته، کانفیگ این کاربران هم ساخته یا
                    از پنل و ساب حذف می‌شود.
                  </p>
                ) : bulkCatTab === "all" ? (
                  <p className="field-hint user-create-cat-hint">
                    همه گروه‌ها انتخاب شدند — روی همه ساخته می‌شود.
                  </p>
                ) : (
                  <p className="field-hint user-create-cat-hint">
                    روی «همه» بزنید تا همه گروه‌ها انتخاب شوند، یا روی نام دسته.
                  </p>
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
                  <p className="field-hint">
                    {bulkStartOnFirstUse
                      ? "از لحظهٔ اولین اتصال شمرده می‌شود"
                      : "۰ = نامحدود"}
                  </p>
                </div>
              </div>
              <button
                type="button"
                className={`settings-switch user-hold-switch${bulkStartOnFirstUse ? " on" : ""}`}
                role="switch"
                aria-checked={bulkStartOnFirstUse}
                disabled={bulkCreate.isPending}
                onClick={() => setBulkStartOnFirstUse((v) => !v)}
              >
                <span className="settings-switch-track" aria-hidden>
                  <span className="settings-switch-knob" />
                </span>
                <span className="user-hold-switch-copy">
                  <strong>شروع از اولین استفاده</strong>
                  <small>
                    {bulkStartOnFirstUse
                      ? "تایمر روز اعتبار بعد از اولین اتصال روی همه پنل‌ها با هم شروع می‌شود"
                      : "خاموش: تایمر از همین الان شروع می‌شود"}
                  </small>
                </span>
              </button>
              {bulkStartOnFirstUse && Number(bulkDays) <= 0 && (
                <p className="error">برای این گزینه باید روز اعتبار بیشتر از صفر باشد</p>
              )}
              <button
                type="button"
                className={`settings-switch user-hold-switch${bulkIncludePromoFirst ? " on" : ""}`}
                role="switch"
                aria-checked={bulkIncludePromoFirst}
                disabled={bulkCreate.isPending}
                onClick={() => setBulkIncludePromoFirst((v) => !v)}
              >
                <span className="settings-switch-track" aria-hidden>
                  <span className="settings-switch-knob" />
                </span>
                <span className="user-hold-switch-copy">
                  <strong>تبلیغ اول</strong>
                  <small>
                    {bulkIncludePromoFirst
                      ? "کانفیگ تبلیغ اول تنظیمات در ساب این کاربران نمایش داده می‌شود"
                      : "تبلیغ اول برای این کاربران در ساب نمی‌آید"}
                  </small>
                </span>
              </button>

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
                    bulkCountN < 1 ||
                    (bulkStartOnFirstUse && Number(bulkDays) <= 0)
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

      {createResult &&
        createPortal(
          <div
            className="modal-backdrop user-bulk-result-backdrop"
            onClick={() => setCreateResult(null)}
          >
            <div
              className="modal user-bulk-result-modal"
              role="dialog"
              aria-modal="true"
              aria-labelledby="create-result-title"
              onClick={(e) => e.stopPropagation()}
            >
              <header className="modal-head">
                <div>
                  <h3 id="create-result-title">کاربر ساخته شد</h3>
                  <p className="modal-sub">
                    لینک ساب آماده‌ است · ساخت کانفیگ روی پنل‌ها در پس‌زمینه
                  </p>
                </div>
                <button
                  type="button"
                  className="modal-close"
                  onClick={() => setCreateResult(null)}
                  aria-label="بستن"
                >
                  ×
                </button>
              </header>

              <p className="user-bulk-result-bg-note">
                مثل ساخت گروهی: پاسخ فوری است و پنل‌ها با سرعت بالا در پس‌زمینه
                ساخته می‌شوند.
              </p>

              <div className="user-bulk-result-meta" style={{ marginBottom: 10 }}>
                <strong dir="ltr">{createResult.username}</strong>
                {createResult.display_name ? (
                  <span>{createResult.display_name}</span>
                ) : null}
              </div>
              <label className="user-bulk-result-all-wrap">
                <span className="user-bulk-result-all-label">لینک ساب</span>
                <textarea
                  id="create-sub-link-box"
                  className="user-bulk-result-all-links"
                  readOnly
                  dir="ltr"
                  rows={3}
                  value={createResult.subscription_url || ""}
                  autoFocus
                  onFocus={(e) => e.currentTarget.select()}
                  onClick={(e) => e.currentTarget.select()}
                />
              </label>

              <div className="user-bulk-result-actions">
                <button
                  type="button"
                  className="btn user-bulk-copy-all-btn"
                  disabled={!createResult.subscription_url}
                  onClick={() => {
                    const url = createResult.subscription_url || "";
                    if (!url) return;
                    const el = document.getElementById(
                      "create-sub-link-box"
                    ) as HTMLTextAreaElement | null;
                    el?.select();
                    copy(url, "create-sub");
                  }}
                >
                  {copied === "create-sub" ? (
                    <>
                      <Check size={16} /> کپی شد
                    </>
                  ) : (
                    <>
                      <Copy size={16} /> کپی لینک ساب
                    </>
                  )}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setCreateResult(null)}
                >
                  بستن
                </button>
              </div>
            </div>
          </div>,
          document.body
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
                    {toFaDigits(bulkResult.total_requested)} با لینک ساب
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

              {bulkResult.remote_bg && (
                <p className="user-bulk-result-bg-note">
                  {bulkResult.message ||
                    "لینک ساب‌ها آماده‌اند — ساخت روی پنل‌ها در پس‌زمینه با سرعت بالا ادامه دارد"}
                </p>
              )}

              {bulkResult.created.length > 0 && (
                <label className="user-bulk-result-all-wrap">
                  <span className="user-bulk-result-all-label">
                    همه ساب‌ها یک‌جا — انتخاب و کپی
                  </span>
                  <textarea
                    id="bulk-all-links-box"
                    className="user-bulk-result-all-links"
                    readOnly
                    dir="ltr"
                    rows={Math.min(14, Math.max(6, bulkResult.created.length))}
                    value={bulkAllLinksText()}
                    autoFocus
                    onFocus={(e) => e.currentTarget.select()}
                    onClick={(e) => e.currentTarget.select()}
                  />
                </label>
              )}

              <div className="user-bulk-result-actions">
                <button
                  type="button"
                  className="btn user-bulk-copy-all-btn"
                  onClick={copyAllBulkLinksOnly}
                  disabled={!bulkResult.created.length}
                >
                  {copied === "bulk-links" ? (
                    <>
                      <Check size={16} /> همه ساب‌ها کپی شد
                    </>
                  ) : (
                    <>
                      <Copy size={16} /> کپی همه ساب‌ها
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
                      <Copy size={15} /> یوزرنیم + لینک
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
                <button
                  type="button"
                  className="btn"
                  onClick={copyAllBulkLinksOnly}
                  disabled={!bulkResult.created.length}
                >
                  {copied === "bulk-links" ? "کپی شد" : "کپی همه ساب‌ها"}
                </button>
                <button type="button" className="btn ghost" onClick={() => setBulkResult(null)}>
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
                if (editUser.status === "on_hold" && Number(editDays) <= 0) return;
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
                  <label>
                    {editUser.status === "on_hold"
                      ? "روز اعتبار از اولین استفاده"
                      : "روز اعتبار از الان"}
                  </label>
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
                  <p className="field-hint">
                    {editUser.status === "on_hold"
                      ? "تایمر هنوز شروع نشده — از اولین اتصال شمرده می‌شود"
                      : "۰ = نامحدود · از لحظه ذخیره محاسبه می‌شود"}
                  </p>
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
                  disabled={
                    saveEdit.isPending ||
                    editGroupIds.length === 0 ||
                    (editUser.status === "on_hold" && Number(editDays) <= 0)
                  }
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
                  مصرف کل باید با جمع ردیف‌های زیر یکی باشد. پنل/گروه حذف‌شده با برچسب «ریمو شده»
                  می‌ماند تا سقف کم نشود. اگر «اختلاف متر» دیدی، با چک مصرف اصلاح می‌شود.
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
                <div className="usage-modal-total usage-modal-total--sub">
                  <span>جمع ردیف‌های پنل</span>
                  <strong>{formatTraffic(usageTotalWeighted, { empty: "۰" })}</strong>
                </div>

                {usageRows.length === 0 ? (
                  <p className="muted" style={{ textAlign: "center", padding: "20px 0" }}>
                    پنل یا مصرف ریمو‌شده برای این کاربر نیست
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
                        <div
                          key={row.key}
                          className={`usage-panel-row${row.banked ? " usage-panel-row--banked" : ""}${row.residual ? " usage-panel-row--residual" : ""}`}
                        >
                          <div className="usage-panel-head">
                            <strong>{row.name}</strong>
                            <span>{formatTraffic(row.weighted, { empty: "۰" })}</span>
                          </div>
                          <div className="usage-panel-bar" aria-hidden>
                            <i style={{ width: `${width}%` }} />
                          </div>
                          <div className="usage-panel-meta">
                            <span>خام: {formatTraffic(row.raw, { empty: "۰" })}</span>
                            {!row.residual ? (
                              <span>ضریب ×{toFaDigits(formatCoeff(row.coefficient))}</span>
                            ) : null}
                            {row.residual ? (
                              <span>در هیچ پنل زنده‌ای نیست — معمولاً تورم متر</span>
                            ) : row.banked ? (
                              <span>ریمو شده · مصرف در سقف می‌ماند</span>
                            ) : (
                              <span>
                                روی پنل:{" "}
                                {row.remoteLimit > 0
                                  ? formatTraffic(row.remoteLimit, { empty: "۰" })
                                  : "نامحدود"}
                              </span>
                            )}
                            {!row.residual ? (
                              <span>
                                {row.banked
                                  ? "بدون کانفیگ"
                                  : `${toFaDigits(row.configs)} کانفیگ`}
                              </span>
                            ) : null}
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
                  فقط گروه‌های همین کاربر. گروه/پنل حذف‌شده = «ریمو شده» با مقدار مصرف‌شده.
                  جمع ردیف‌ها باید با مصرف کل یکی باشد؛ اختلاف جدا ردیف می‌شود.
                </p>

                <div className="usage-modal-total">
                  <span>مصرف کل Astra</span>
                  <strong>
                    {formatTraffic(usageUser.used_traffic || groupUsageTotal, {
                      empty: "۰",
                    })}
                    <small> / {formatDataLimit(usageUser.data_limit)}</small>
                  </strong>
                </div>
                <div className="usage-modal-total usage-modal-total--sub">
                  <span>جمع ردیف‌های گروه</span>
                  <strong>{formatTraffic(groupUsageTotal, { empty: "۰" })}</strong>
                </div>

                {groupUsage.length === 0 ? (
                  <p className="muted" style={{ textAlign: "center", padding: "20px 0" }}>
                    گروهی برای این کاربر نیست
                  </p>
                ) : (
                  <div className="usage-panel-list">
                    {groupUsage.map((row) => {
                      const width = Math.min(100, (row.weighted / groupUsageMax) * 100);
                      const astraTotal =
                        Number(usageUser.used_traffic) || groupUsageTotal;
                      const shareOfAstra =
                        astraTotal > 0
                          ? Math.min(
                              100,
                              Math.round((row.weighted / astraTotal) * 100)
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
                        <div
                          key={`${row.groupId}-${row.banked ? "b" : "l"}-${row.residual ? "r" : "n"}`}
                          className={`usage-panel-row${row.banked ? " usage-panel-row--banked" : ""}${row.residual ? " usage-panel-row--residual" : ""}`}
                        >
                          <div className="usage-panel-head">
                            <strong>{row.name}</strong>
                            <span>{formatTraffic(row.weighted, { empty: "۰" })}</span>
                          </div>
                          <div className="usage-panel-bar" aria-hidden>
                            <i style={{ width: `${width}%` }} />
                          </div>
                          <div className="usage-panel-meta">
                            <span>خام: {formatTraffic(row.raw, { empty: "۰" })}</span>
                            {!row.residual ? (
                              <span>ضریب ×{toFaDigits(coeffLabel)}</span>
                            ) : null}
                            {!row.residual ? (
                              <span>{toFaDigits(row.panels)} پنل</span>
                            ) : null}
                            <span>
                              {row.residual
                                ? "در هیچ گروه زنده‌ای نیست — معمولاً تورم متر"
                                : row.banked
                                  ? "ریمو شده · مصرف در سقف می‌ماند"
                                  : `${toFaDigits(row.configs)} کانفیگ`}
                            </span>
                            <span>{toFaDigits(shareOfGroups)}٪ از ردیف‌ها</span>
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
                  کاربر فوراً از Astra حذف می‌شود؛ کانفیگ‌ها از همه پنل‌ها موازی پاک می‌شوند
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
                {remove.isPending ? "در حال حذف…" : "بله، حذف شود"}
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
                {errorAccounts.map((a) => {
                  const capacity = isCapacityAccountError(a.error_message);
                  const softTimeout = isSoftTimeoutAccountError(a.error_message);
                  const outboundOff = isOutboundDisabledError(a.error_message);
                  const soft = capacity || softTimeout || outboundOff;
                  return (
                  <div key={a.id || a.panel_id} className="user-err-item">
                    <div className="user-err-head">
                      <strong>{a.panel_name || `پنل #${a.panel_id}`}</strong>
                      <span className={`badge ${soft ? "warn" : "err"}`}>
                        {capacity
                          ? "ظرفیت پر"
                          : outboundOff
                            ? "اوتباند خاموش"
                            : softTimeout
                              ? "کند / تایم‌اوت"
                              : "خطا"}
                      </span>
                      {a.panel_type && <span className="badge muted">{a.panel_type}</span>}
                    </div>
                    <pre className="user-err-body" dir="auto">
                      {formatPanelErrorDetail(a, errorsUser.username)}
                    </pre>
                  </div>
                  );
                })}
              </div>
            )}

            <footer className="modal-foot">
              <button type="button" className="btn ghost" onClick={() => setErrorsUser(null)}>
                بستن
              </button>
              {errorAccounts.length > 0 && (
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
