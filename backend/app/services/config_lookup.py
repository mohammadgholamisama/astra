"""Resolve a pasted share-link / sub remark to Astra panel + sales group.

Priority: the group's ``config_tag`` (text inside ``#N. TAG … | user``) must
win over bare host:port / panel-name heuristics — those often point at the
wrong sales group when the same outbound sits in several groups.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.http_utils import is_share_link
from app.models.astra_outbound import AstraOutbound
from app.models.group import Group, GroupPanel
from app.models.group_config import GroupConfigItem
from app.models.user import PanelAccount, PanelAccountStatus
from app.schemas.config_lookup import (
    ConfigLookupHit,
    ConfigLookupOut,
    ConfigLookupParsed,
)
from app.services.config_locations import (
    extract_link_client_id,
    extract_link_endpoint,
    extract_link_match_host,
    match_host_sni_only,
    normalize_match_host,
    try_normalize_endpoint,
)
from app.services.group_configs import _parse_link_meta

logger = logging.getLogger("astra.config_lookup")

_LINK_RE = re.compile(
    r"(?:vless|vmess|trojan|ss|shadowsocks|hysteria2?|hy2|tuic|wireguard)://[^\s<>\"']+",
    re.IGNORECASE,
)

# Cap rows touched per path so lookup stays interactive even on huge DBs.
_MAX_UUID_ACCOUNTS = 40
_MAX_LINK_SCAN = 80
_MAX_OUTBOUNDS = 200
_MAX_TAG_GROUPS = 40

# ``#1. TAG 8.6G | user 🇦🇱``  or  ``#1. 8.6G | user``
_ASTRA_REMARK_RE = re.compile(
    r"^#(?P<cid>\d+)\.\s*(?P<body>.+?)\s*\|\s*(?P<user>.+?)\s*$",
    re.UNICODE,
)
_REMAIN_TOKEN_RE = re.compile(
    r"(?P<remain>\d+(?:[.,]\d+)?\s*[KMGT]?B?|∞|نامحدود)$",
    re.IGNORECASE | re.UNICODE,
)


def _extract_links(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _LINK_RE.finditer(raw):
        link = m.group(0).rstrip(").,]}\"';")
        if not is_share_link(link):
            continue
        if link in seen:
            continue
        seen.add(link)
        found.append(link)
    if not found and is_share_link(raw.splitlines()[0].strip()):
        found.append(raw.splitlines()[0].strip())
    return found


def _sni_core(match_host: str) -> str:
    try:
        return normalize_match_host(match_host_sni_only(match_host or ""))
    except ValueError:
        return (match_host_sni_only(match_host or "") or "").strip().lower()


def _fp_of_link(link: str) -> tuple[str, int, str] | None:
    ep = extract_link_endpoint(link)
    if not ep:
        return None
    host, port = ep
    match = extract_link_match_host(link) or ""
    norm = try_normalize_endpoint(host, port, match)
    if not norm:
        return None
    h, p, m = norm
    return (h, int(p), m or "")


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", (s or "").lower())


def _strip_outer_parens(s: str) -> str:
    t = (s or "").strip()
    while len(t) >= 2 and t[0] in "([{" and t[-1] in ")]}":
        t = t[1:-1].strip()
    return t


def parse_astra_remark(remark: str | None) -> dict[str, str]:
    """Pull config_tag / remain / user from an Astra subscription remark."""
    raw = unquote((remark or "").strip())
    if not raw:
        return {"tag": "", "remain": "", "user": "", "raw": ""}
    m = _ASTRA_REMARK_RE.match(raw)
    if not m:
        # Bare tag paste (admin typed only the name from the sub)
        tag = _strip_outer_parens(raw)
        return {"tag": tag, "remain": "", "user": "", "raw": raw}
    body = " ".join((m.group("body") or "").split()).strip()
    user = " ".join((m.group("user") or "").split()).strip()
    # Drop trailing flag emoji from username for display matching
    remain = ""
    tag = body
    rm = _REMAIN_TOKEN_RE.search(body)
    if rm:
        remain = (rm.group("remain") or "").strip()
        tag = body[: rm.start()].strip()
    tag = _strip_outer_parens(tag)
    return {"tag": tag, "remain": remain, "user": user, "raw": raw}


def _remark_close(a: str | None, b: str | None) -> bool:
    x = (a or "").strip().lower()
    y = (b or "").strip().lower()
    if not x or not y:
        return False
    return x == y or x in y or y in x


def _tag_score(
    query_tag: str,
    *,
    group_config_tag: str | None = None,
    group_name: str | None = None,
    source_remark: str | None = None,
) -> int:
    """How well a hit's group matches the remark's config_tag. Higher = better."""
    q = _norm_key(_strip_outer_parens(query_tag or ""))
    if not q or len(q) < 2:
        return 0
    gtag = _norm_key(_strip_outer_parens(group_config_tag or ""))
    gname = _norm_key(group_name or "")
    # Prefer exact group.config_tag — that is what build_config_remark embeds
    if gtag and gtag == q:
        return 100
    if gtag and (q in gtag or gtag in q):
        return 85
    if gname and gname == q:
        return 70
    if gname and (q in gname or gname in q):
        return 55
    # source_remark may be the full ``#N. TAG …`` or just a sample label
    parsed = parse_astra_remark(source_remark)
    s_tag = _norm_key(_strip_outer_parens(parsed.get("tag") or ""))
    if s_tag and s_tag == q:
        return 90
    if s_tag and (q in s_tag or s_tag in q):
        return 75
    s_raw = _norm_key(source_remark or "")
    if s_raw and (q in s_raw or s_raw in q):
        return 40
    return 0


def _parse_query(link: str, *, fallback_remark: str | None = None) -> ConfigLookupParsed:
    proto, remark, _ = _parse_link_meta(link)
    rem = (remark or "").strip() or (fallback_remark or "").strip() or None
    parsed_tag = parse_astra_remark(rem).get("tag") or None
    fp = _fp_of_link(link)
    host = ""
    port = 0
    match = ""
    if fp:
        host, port, match = fp[0], fp[1], fp[2]
    preview = link if len(link) <= 120 else link[:117] + "…"
    return ConfigLookupParsed(
        link_preview=preview,
        protocol=(proto or None),
        remark=rem,
        config_tag=parsed_tag or None,
        host=str(host or ""),
        port=int(port or 0),
        match_host=match or "",
    )


def _quality(
    q_fp: tuple[str, int, str],
    c_fp: tuple[str, int, str],
    *,
    uuid_match: bool,
) -> str | None:
    """Match only when host:port agree; SNI/header must not contradict."""
    qh, qp, qm = q_fp
    ch, cp, cm = c_fp
    if qh != ch or qp != cp:
        return None
    q_match = (qm or "").strip()
    c_match = (cm or "").strip()
    if q_match == c_match:
        return "exact"
    if uuid_match:
        return "uuid"
    qs = _sni_core(q_match)
    cs = _sni_core(c_match)
    if qs and cs and qs == cs:
        return "sni"
    if not q_match and not c_match:
        return "host_port"
    return None


_QUALITY_RANK = {
    "tag": 0,
    "exact": 1,
    "uuid": 2,
    "uri": 2,
    "sni": 3,
    "host_port": 4,
}

# Keep letter+digit units together: Hajmi6, Tunnel (not Hajmi / 6 separately)
_STEM_RE = re.compile(r"[A-Za-z\u0600-\u06FF]+\d*", re.UNICODE)


def _panel_stems(panel_name: str) -> list[str]:
    return [m.group(0).lower() for m in _STEM_RE.finditer(panel_name or "") if len(m.group(0)) >= 2]


def _group_fit(panel_name: str, group_name: str | None) -> tuple[int, int, int]:
    """Last-resort tie-break only — never overrides config_tag match."""
    gn = (group_name or "").strip()
    if not gn:
        return (0, 0, 0)
    stems = _panel_stems(panel_name)
    if not stems:
        return (0, 0, 0)
    gkey = _norm_key(gn)
    hits = sum(1 for s in stems if s in gkey)
    pkey = _norm_key(panel_name)
    blob = 1 if pkey and pkey in gkey else 0
    return (hits, blob, -abs(len(gn) - len(panel_name or "")))


def _pick_best_group_hits(pool: list[dict[str, Any]], query_tag: str) -> list[dict[str, Any]]:
    """One panel + same fingerprint in several groups → keep the best group."""
    if len(pool) <= 1:
        return pool
    by_fp: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for h in pool:
        key = (
            str(h.get("host") or ""),
            int(h.get("port") or 0),
            str(h.get("match_host") or ""),
        )
        by_fp.setdefault(key, []).append(h)
    out: list[dict[str, Any]] = []
    for group in by_fp.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        best = max(
            group,
            key=lambda h: (
                _tag_score(
                    query_tag,
                    group_config_tag=h.get("group_config_tag"),
                    group_name=h.get("group_name"),
                    source_remark=h.get("source_remark"),
                ),
                int(h.get("accounts_count") or 0),
                _group_fit(str(h.get("panel_name") or ""), h.get("group_name")),
                -(int(h["group_id"]) if h.get("group_id") is not None else 10**9),
            ),
        )
        out.append(best)
    return out


def _panel_type_str(panel: Any) -> str | None:
    if panel is None:
        return None
    t = getattr(panel, "type", None)
    if t is None:
        return None
    return t.value if hasattr(t, "value") else str(t or "") or None


def _group_meta(group: Group | None) -> tuple[str | None, str | None, str | None]:
    if group is None:
        return None, None, None
    name = (str(getattr(group, "name", "") or "") or None)
    tag = (str(getattr(group, "config_tag", "") or "").strip() or None)
    kind = (str(getattr(group, "order_kind", "") or "").strip().lower() or None)
    return name, tag, kind


async def lookup_config(db: AsyncSession, text: str) -> ConfigLookupOut:
    raw_text = (text or "").strip()
    links = _extract_links(raw_text)

    # Remark / tag paste without a share link
    if not links:
        tag_info = parse_astra_remark(raw_text)
        q_tag = (tag_info.get("tag") or "").strip()
        if not q_tag:
            return ConfigLookupOut(
                links_found=0,
                detail="لینک کانفیگ یا نام روی ساب (مثل #1. تگ … | user) بچسبانید",
            )
        parsed = ConfigLookupParsed(
            link_preview=raw_text[:120],
            remark=tag_info.get("raw") or raw_text,
            config_tag=q_tag,
        )
        buckets: dict[tuple[int, int | None], dict[str, Any]] = {}
        await _add_hits_by_config_tag(db, buckets, q_tag)
        return _finalize(buckets, parsed, links=[], query_tag=q_tag)

    query_link = links[0]
    # Remark may sit next to the link in the paste
    side_remark = None
    for line in raw_text.splitlines():
        line = line.strip()
        if line and not is_share_link(line) and "://" not in line:
            side_remark = line
            break
    parsed = _parse_query(query_link, fallback_remark=side_remark)
    q_tag = (parsed.config_tag or "").strip()
    if not q_tag and side_remark:
        q_tag = (parse_astra_remark(side_remark).get("tag") or "").strip()
        if q_tag:
            parsed.config_tag = q_tag

    q_fp = _fp_of_link(query_link)
    if not q_fp:
        # Still try tag-only if remark is present
        if q_tag:
            buckets = {}
            await _add_hits_by_config_tag(db, buckets, q_tag)
            return _finalize(buckets, parsed, links, query_tag=q_tag)
        return ConfigLookupOut(
            parsed=parsed,
            links_found=len(links),
            detail="نمی‌توان host/port این لینک را خواند",
        )
    q_uuid = (extract_link_client_id(query_link) or "").strip().lower()
    q_host, q_port, _q_match = q_fp
    raw_ep = extract_link_endpoint(query_link)

    buckets = {}

    def _add_hit(
        *,
        panel_id: int,
        panel_name: str,
        panel_type: str | None,
        panel_is_active: bool,
        group_id: int | None,
        group_name: str | None,
        group_config_tag: str | None = None,
        order_kind: str | None = None,
        quality: str,
        source_remark: str | None,
        host: str,
        port: int,
        match_host: str,
        sample_user: str | None = None,
        bump_account: bool = False,
    ) -> None:
        key = (int(panel_id), int(group_id) if group_id is not None else None)
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = {
                "panel_id": int(panel_id),
                "panel_name": panel_name,
                "panel_type": panel_type,
                "panel_is_active": bool(panel_is_active),
                "group_id": int(group_id) if group_id is not None else None,
                "group_name": group_name,
                "group_config_tag": group_config_tag,
                "order_kind": order_kind,
                "match_quality": quality,
                "source_remark": source_remark,
                "host": host,
                "port": port,
                "match_host": match_host,
                "accounts_count": 1 if (sample_user or bump_account) else 0,
                "sample_users": [sample_user] if sample_user else [],
            }
            return
        if _QUALITY_RANK.get(quality, 9) < _QUALITY_RANK.get(
            str(cur.get("match_quality") or ""), 9
        ):
            cur["match_quality"] = quality
            cur["source_remark"] = source_remark or cur.get("source_remark")
            cur["host"] = host
            cur["port"] = port
            cur["match_host"] = match_host
        elif source_remark and not cur.get("source_remark"):
            cur["source_remark"] = source_remark
        if group_config_tag and not cur.get("group_config_tag"):
            cur["group_config_tag"] = group_config_tag
        if order_kind and not cur.get("order_kind"):
            cur["order_kind"] = order_kind
        if group_name and not cur.get("group_name"):
            cur["group_name"] = group_name
        if sample_user or bump_account:
            cur["accounts_count"] = int(cur.get("accounts_count") or 0) + 1
            if sample_user:
                users: list[str] = list(cur.get("sample_users") or [])
                if sample_user not in users and len(users) < 5:
                    users.append(sample_user)
                    cur["sample_users"] = users

    def _hit_from_panel(
        panel: Any,
        *,
        group_id: int | None,
        group_name: str | None,
        group_config_tag: str | None = None,
        order_kind: str | None = None,
        quality: str,
        source_remark: str | None,
        c_fp: tuple[str, int, str],
        sample_user: str | None = None,
        bump_account: bool = False,
    ) -> None:
        if panel is None:
            return
        _add_hit(
            panel_id=int(panel.id),
            panel_name=str(panel.name or f"#{panel.id}"),
            panel_type=_panel_type_str(panel),
            panel_is_active=bool(getattr(panel, "is_active", True)),
            group_id=group_id,
            group_name=group_name,
            group_config_tag=group_config_tag,
            order_kind=order_kind,
            quality=quality,
            source_remark=source_remark,
            host=c_fp[0],
            port=c_fp[1],
            match_host=c_fp[2],
            sample_user=sample_user,
            bump_account=bump_account,
        )

    # ── 1) UUID → PanelAccount ──────────────────────────────────────────────
    if q_uuid:
        uuid_rows = await db.execute(
            select(PanelAccount)
            .where(
                PanelAccount.status != PanelAccountStatus.deleted,
                func.lower(PanelAccount.uuid) == q_uuid,
            )
            .options(
                selectinload(PanelAccount.panel),
                selectinload(PanelAccount.user),
            )
            .limit(_MAX_UUID_ACCOUNTS)
        )
        uuid_accounts = list(uuid_rows.scalars().unique().all())
        gids = {
            int(a.group_id)
            for a in uuid_accounts
            if getattr(a, "group_id", None) is not None
        }
        groups_by_id: dict[int, Group] = {}
        if gids:
            g_rows = await db.execute(select(Group).where(Group.id.in_(sorted(gids))))
            groups_by_id = {int(g.id): g for g in g_rows.scalars().all()}

        for account in uuid_accounts:
            panel = account.__dict__.get("panel")
            if panel is None:
                continue
            gid = getattr(account, "group_id", None)
            group = groups_by_id.get(int(gid)) if gid is not None else None
            g_name, g_tag, g_kind = _group_meta(group)
            uname = ""
            user = account.__dict__.get("user")
            if user is not None:
                uname = str(getattr(user, "username", "") or "")

            quality = "uuid"
            remark: str | None = None
            c_fp = q_fp
            for raw in account.links or []:
                link = str(raw or "").strip()
                if not link or not is_share_link(link):
                    continue
                fp = _fp_of_link(link)
                if not fp:
                    continue
                q = _quality(q_fp, fp, uuid_match=True)
                if not q:
                    continue
                quality = q
                c_fp = fp
                _p, rem, _ = _parse_link_meta(link)
                remark = (rem or "").strip() or None
                break

            # Never paste the query remark as "نام روی پنل" — that hides mismatches
            _hit_from_panel(
                panel,
                group_id=int(gid) if gid is not None else None,
                group_name=g_name,
                group_config_tag=g_tag,
                order_kind=g_kind,
                quality=quality,
                source_remark=remark,
                c_fp=c_fp,
                sample_user=uname or None,
                bump_account=True,
            )

        # UUID is definitive only when no config_tag is known, OR the account's
        # group actually carries that tag. Otherwise keep scanning.
        if buckets and not q_tag:
            return _finalize(buckets, parsed, links, query_tag=q_tag)
        if buckets and q_tag:
            tagged_ok = any(
                _tag_score(
                    q_tag,
                    group_config_tag=h.get("group_config_tag"),
                    group_name=h.get("group_name"),
                    source_remark=h.get("source_remark"),
                )
                >= 55
                for h in buckets.values()
            )
            if tagged_ok:
                return _finalize(buckets, parsed, links, query_tag=q_tag)

    # ── 1b) Groups whose config_tag matches the sub remark ──────────────────
    if q_tag:
        await _add_hits_by_config_tag(
            db,
            buckets,
            q_tag,
            prefer_fp=q_fp,
            add_hit=_add_hit,
        )

    # ── 2) GroupConfigItem sample links ─────────────────────────────────────
    item_like = [
        cast(GroupConfigItem.sample_link, String).contains(f"{q_host}:{q_port}"),
    ]
    if raw_ep:
        alt = f"{raw_ep[0]}:{raw_ep[1]}"
        if alt != f"{q_host}:{q_port}":
            item_like.append(cast(GroupConfigItem.sample_link, String).contains(alt))
    item_rows = await db.execute(
        select(GroupConfigItem)
        .where(or_(*item_like))
        .options(
            selectinload(GroupConfigItem.panel),
            selectinload(GroupConfigItem.group),
        )
    )
    for item in item_rows.scalars().unique().all():
        sample = str(item.sample_link or "").strip()
        if not sample or not is_share_link(sample):
            continue
        c_fp = _fp_of_link(sample)
        if not c_fp:
            continue
        c_uuid = (extract_link_client_id(sample) or "").strip().lower()
        uuid_ok = bool(q_uuid and c_uuid and q_uuid == c_uuid)
        quality = _quality(q_fp, c_fp, uuid_match=uuid_ok)
        if not quality:
            continue
        panel = item.__dict__.get("panel")
        group = item.__dict__.get("group")
        g_name, g_tag, g_kind = _group_meta(group)
        rem = (item.label or "").strip() or None
        if not rem:
            _p, rem2, _ = _parse_link_meta(sample)
            rem = (rem2 or "").strip() or None
        # Boost to tag quality when this group's tag matches the query
        if q_tag and _tag_score(q_tag, group_config_tag=g_tag, group_name=g_name, source_remark=rem) >= 55:
            quality = "tag"
        _hit_from_panel(
            panel,
            group_id=int(item.group_id) if item.group_id is not None else None,
            group_name=g_name,
            group_config_tag=g_tag,
            order_kind=g_kind,
            quality=quality,
            source_remark=rem,
            c_fp=c_fp,
        )

    # ── 3) Astra outbound pool ──────────────────────────────────────────────
    host_variants = {q_host, q_host.lower()}
    if raw_ep:
        host_variants.add(str(raw_ep[0] or "").strip().lower())
        host_variants.add(str(raw_ep[0] or "").strip())
    host_variants.discard("")

    ob_filters = [AstraOutbound.port == int(q_port)]
    if host_variants:
        ob_filters.append(
            or_(
                AstraOutbound.host.in_(sorted(host_variants)),
                func.lower(AstraOutbound.host).in_(
                    sorted({h.lower() for h in host_variants})
                ),
            )
        )
    ob_rows = await db.execute(
        select(AstraOutbound)
        .where(*ob_filters)
        .options(selectinload(AstraOutbound.panel))
        .limit(_MAX_OUTBOUNDS)
    )
    for ob in ob_rows.scalars().unique().all():
        panel = ob.__dict__.get("panel")
        if panel is None:
            continue
        uri = str(ob.uri or "").strip()
        if not uri or not is_share_link(uri):
            continue
        c_fp = _fp_of_link(uri)
        if not c_fp:
            continue
        c_uuid = (extract_link_client_id(uri) or "").strip().lower()
        uuid_ok = bool(q_uuid and c_uuid and q_uuid == c_uuid)
        quality = _quality(q_fp, c_fp, uuid_match=uuid_ok)
        if not quality and uri.strip() == query_link.strip():
            quality = "uri"
        if not quality:
            continue
        rem = (ob.remark or "").strip() or None
        if not rem:
            _p, rem2, _ = _parse_link_meta(uri)
            rem = (rem2 or "").strip() or None
        _hit_from_panel(
            panel,
            group_id=None,
            group_name=None,
            quality=quality if quality != "uri" else "exact",
            source_remark=rem,
            c_fp=c_fp,
        )

    # ── 4) Fallback: SQL prefilter on account.links ─────────────────────────
    if not buckets or (q_tag and not any(
        _tag_score(
            q_tag,
            group_config_tag=h.get("group_config_tag"),
            group_name=h.get("group_name"),
            source_remark=h.get("source_remark"),
        )
        for h in buckets.values()
    )):
        needles = [f"{q_host}:{q_port}"]
        if raw_ep:
            rh, rp = raw_ep
            alt = f"{rh}:{rp}"
            if alt not in needles:
                needles.append(alt)
        like_clauses = [
            cast(PanelAccount.links, String).contains(n) for n in needles if n
        ]
        if q_uuid:
            like_clauses.append(cast(PanelAccount.links, String).contains(q_uuid))
        if like_clauses:
            acc_rows = await db.execute(
                select(PanelAccount)
                .where(
                    PanelAccount.status != PanelAccountStatus.deleted,
                    or_(*like_clauses),
                )
                .options(
                    selectinload(PanelAccount.panel),
                    selectinload(PanelAccount.user),
                )
                .limit(_MAX_LINK_SCAN)
            )
            accounts = list(acc_rows.scalars().unique().all())
            gids = {
                int(a.group_id)
                for a in accounts
                if getattr(a, "group_id", None) is not None
            }
            groups_by_id = {}
            if gids:
                g_rows = await db.execute(
                    select(Group).where(Group.id.in_(sorted(gids)))
                )
                groups_by_id = {int(g.id): g for g in g_rows.scalars().all()}

            for account in accounts:
                panel = account.__dict__.get("panel")
                if panel is None:
                    continue
                gid = getattr(account, "group_id", None)
                group = groups_by_id.get(int(gid)) if gid is not None else None
                g_name, g_tag, g_kind = _group_meta(group)
                uname = ""
                user = account.__dict__.get("user")
                if user is not None:
                    uname = str(getattr(user, "username", "") or "")
                for raw in account.links or []:
                    link = str(raw or "").strip()
                    if not link or not is_share_link(link):
                        continue
                    c_fp = _fp_of_link(link)
                    if not c_fp:
                        continue
                    c_uuid = (extract_link_client_id(link) or "").strip().lower()
                    uuid_ok = bool(q_uuid and c_uuid and q_uuid == c_uuid)
                    quality = _quality(q_fp, c_fp, uuid_match=uuid_ok)
                    if not quality:
                        continue
                    _p, rem, _ = _parse_link_meta(link)
                    if q_tag and _tag_score(
                        q_tag,
                        group_config_tag=g_tag,
                        group_name=g_name,
                        source_remark=(rem or "").strip() or None,
                    ) >= 55:
                        quality = "tag"
                    _hit_from_panel(
                        panel,
                        group_id=int(gid) if gid is not None else None,
                        group_name=g_name,
                        group_config_tag=g_tag,
                        order_kind=g_kind,
                        quality=quality,
                        source_remark=(rem or "").strip() or None,
                        c_fp=c_fp,
                        sample_user=uname or None,
                        bump_account=True,
                    )
                    break

    return _finalize(buckets, parsed, links, query_tag=q_tag)


async def _add_hits_by_config_tag(
    db: AsyncSession,
    buckets: dict[tuple[int, int | None], dict[str, Any]],
    q_tag: str,
    *,
    prefer_fp: tuple[str, int, str] | None = None,
    add_hit: Any | None = None,
) -> None:
    """Find sales groups whose config_tag / name matches the sub remark tag."""
    tag = _strip_outer_parens((q_tag or "").strip())
    if not tag or len(tag) < 2:
        return
    needle = tag[:80]
    rows = await db.execute(
        select(Group)
        .where(
            or_(
                Group.config_tag.ilike(f"%{needle}%"),
                Group.name.ilike(f"%{needle}%"),
            )
        )
        .limit(_MAX_TAG_GROUPS)
    )
    groups = list(rows.scalars().all())
    # Also try without spaces / with normalized compare in Python for Persian
    if not groups:
        all_g = await db.execute(
            select(Group).where(
                or_(Group.config_tag.isnot(None), Group.name.isnot(None))
            ).limit(500)
        )
        groups = [
            g
            for g in all_g.scalars().all()
            if _tag_score(
                tag,
                group_config_tag=getattr(g, "config_tag", None),
                group_name=getattr(g, "name", None),
            )
            >= 55
        ][:_MAX_TAG_GROUPS]

    scored: list[tuple[int, Group]] = []
    for g in groups:
        sc = _tag_score(
            tag,
            group_config_tag=getattr(g, "config_tag", None),
            group_name=getattr(g, "name", None),
        )
        if sc >= 55:
            scored.append((sc, g))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return

    gids = [int(g.id) for _sc, g in scored]
    gp_rows = await db.execute(
        select(GroupPanel)
        .where(GroupPanel.group_id.in_(gids))
        .options(selectinload(GroupPanel.panel))
    )
    panels_by_gid: dict[int, list[Any]] = {}
    for gp in gp_rows.scalars().unique().all():
        panels_by_gid.setdefault(int(gp.group_id), []).append(gp)

    # Optional: sample item label under these groups
    item_rows = await db.execute(
        select(GroupConfigItem)
        .where(GroupConfigItem.group_id.in_(gids))
        .limit(200)
    )
    label_by_gid: dict[int, str] = {}
    for item in item_rows.scalars().all():
        gid = int(item.group_id)
        if gid in label_by_gid:
            continue
        lab = (item.label or "").strip()
        if lab:
            label_by_gid[gid] = lab

    def _default_add(**kwargs: Any) -> None:
        key = (
            int(kwargs["panel_id"]),
            int(kwargs["group_id"]) if kwargs.get("group_id") is not None else None,
        )
        if key in buckets:
            cur = buckets[key]
            if not cur.get("group_config_tag"):
                cur["group_config_tag"] = kwargs.get("group_config_tag")
            if not cur.get("order_kind"):
                cur["order_kind"] = kwargs.get("order_kind")
            if _QUALITY_RANK.get(str(kwargs.get("quality") or ""), 9) < _QUALITY_RANK.get(
                str(cur.get("match_quality") or ""), 9
            ):
                cur["match_quality"] = kwargs["quality"]
            return
        buckets[key] = {
            "panel_id": int(kwargs["panel_id"]),
            "panel_name": kwargs["panel_name"],
            "panel_type": kwargs.get("panel_type"),
            "panel_is_active": bool(kwargs.get("panel_is_active", True)),
            "group_id": kwargs.get("group_id"),
            "group_name": kwargs.get("group_name"),
            "group_config_tag": kwargs.get("group_config_tag"),
            "order_kind": kwargs.get("order_kind"),
            "match_quality": kwargs.get("quality") or "tag",
            "source_remark": kwargs.get("source_remark"),
            "host": kwargs.get("host") or (prefer_fp[0] if prefer_fp else ""),
            "port": int(kwargs.get("port") or (prefer_fp[1] if prefer_fp else 0)),
            "match_host": kwargs.get("match_host")
            or (prefer_fp[2] if prefer_fp else ""),
            "accounts_count": 0,
            "sample_users": [],
        }

    put = add_hit or _default_add

    for sc, group in scored:
        g_name, g_tag, g_kind = _group_meta(group)
        gps = panels_by_gid.get(int(group.id), [])
        src = label_by_gid.get(int(group.id)) or (
            f"{g_tag}" if g_tag else g_name
        )
        if not gps:
            # Group with matching tag but no panels yet — still surface it
            # via a synthetic placeholder only if we have nothing else for it
            continue
        for gp in gps:
            panel = gp.__dict__.get("panel")
            if panel is None:
                continue
            if add_hit is not None:
                # Prefer_fp host used so finalize can still collapse by fp
                host = prefer_fp[0] if prefer_fp else ""
                port = prefer_fp[1] if prefer_fp else 0
                match = prefer_fp[2] if prefer_fp else ""
                add_hit(
                    panel_id=int(panel.id),
                    panel_name=str(panel.name or f"#{panel.id}"),
                    panel_type=_panel_type_str(panel),
                    panel_is_active=bool(getattr(panel, "is_active", True)),
                    group_id=int(group.id),
                    group_name=g_name,
                    group_config_tag=g_tag,
                    order_kind=g_kind,
                    quality="tag",
                    source_remark=src,
                    host=host,
                    port=port,
                    match_host=match,
                )
            else:
                put(
                    panel_id=int(panel.id),
                    panel_name=str(panel.name or f"#{panel.id}"),
                    panel_type=_panel_type_str(panel),
                    panel_is_active=bool(getattr(panel, "is_active", True)),
                    group_id=int(group.id),
                    group_name=g_name,
                    group_config_tag=g_tag,
                    order_kind=g_kind,
                    quality="tag",
                    source_remark=src,
                )


def _finalize(
    buckets: dict[tuple[int, int | None], dict[str, Any]],
    parsed: ConfigLookupParsed,
    links: list[str],
    *,
    query_tag: str = "",
) -> ConfigLookupOut:
    raw_hits = list(buckets.values())
    q_tag = (query_tag or parsed.config_tag or "").strip()
    q_remark = (parsed.remark or "").strip()

    # Same panel with/without group → keep rows that name a sales group
    by_panel: dict[int, list[dict[str, Any]]] = {}
    for h in raw_hits:
        by_panel.setdefault(int(h["panel_id"]), []).append(h)
    collapsed: list[dict[str, Any]] = []
    for _pid, lst in by_panel.items():
        with_group = [x for x in lst if x.get("group_id") is not None]
        pool = with_group or lst
        # If query has a config_tag, prefer groups that carry it before quality
        if q_tag:
            tagged = [
                x
                for x in pool
                if _tag_score(
                    q_tag,
                    group_config_tag=x.get("group_config_tag"),
                    group_name=x.get("group_name"),
                    source_remark=x.get("source_remark"),
                )
                >= 55
            ]
            if tagged:
                pool = tagged
        best_local = min(
            (_QUALITY_RANK.get(str(x.get("match_quality") or ""), 9) for x in pool),
            default=9,
        )
        tier = [
            x
            for x in pool
            if _QUALITY_RANK.get(str(x.get("match_quality") or ""), 9) == best_local
        ]
        collapsed.extend(_pick_best_group_hits(tier, q_tag))

    # Global: if any hit matches the remark tag, drop the rest
    if q_tag and collapsed:
        tagged = [
            h
            for h in collapsed
            if _tag_score(
                q_tag,
                group_config_tag=h.get("group_config_tag"),
                group_name=h.get("group_name"),
                source_remark=h.get("source_remark"),
            )
            >= 55
        ]
        if tagged:
            collapsed = tagged

    # Keep strongest match-quality tier among survivors
    if collapsed:
        best = min(
            _QUALITY_RANK.get(str(h.get("match_quality") or ""), 9) for h in collapsed
        )
        collapsed = [
            h
            for h in collapsed
            if _QUALITY_RANK.get(str(h.get("match_quality") or ""), 9) == best
        ]

    # Remark closeness as secondary filter when still ambiguous
    if q_remark and len(collapsed) > 1:
        remarked = [
            h
            for h in collapsed
            if _remark_close(str(h.get("source_remark") or ""), q_remark)
            or _remark_close(str(h.get("group_config_tag") or ""), q_remark)
            or _remark_close(str(h.get("group_name") or ""), q_remark)
            or (
                q_tag
                and _tag_score(
                    q_tag,
                    group_config_tag=h.get("group_config_tag"),
                    group_name=h.get("group_name"),
                    source_remark=h.get("source_remark"),
                )
                >= 55
            )
        ]
        if remarked:
            collapsed = remarked

    if len(collapsed) > 1:
        with_acc = [h for h in collapsed if int(h.get("accounts_count") or 0) > 0]
        if with_acc:
            collapsed = with_acc

    hits = sorted(
        collapsed,
        key=lambda h: (
            -_tag_score(
                q_tag,
                group_config_tag=h.get("group_config_tag"),
                group_name=h.get("group_name"),
                source_remark=h.get("source_remark"),
            ),
            _QUALITY_RANK.get(str(h.get("match_quality") or ""), 9),
            -int(h.get("accounts_count") or 0),
            str(h.get("panel_name") or ""),
            str(h.get("group_name") or ""),
        ),
    )
    out_hits = [
        ConfigLookupHit(
            panel_id=int(h["panel_id"]),
            panel_name=str(h["panel_name"]),
            panel_type=h.get("panel_type"),
            panel_is_active=bool(h.get("panel_is_active", True)),
            group_id=h.get("group_id"),
            group_name=h.get("group_name"),
            group_config_tag=h.get("group_config_tag"),
            order_kind=h.get("order_kind"),
            source_remark=h.get("source_remark"),
            match_quality=str(h.get("match_quality") or "exact"),
            host=str(h.get("host") or ""),
            port=int(h.get("port") or 0),
            match_host=str(h.get("match_host") or ""),
            accounts_count=int(h.get("accounts_count") or 0),
            sample_users=list(h.get("sample_users") or []),
        )
        for h in hits
    ]

    detail = None
    if not out_hits:
        detail = (
            "این کانفیگ روی هیچ پنل/گروهی در آسترا پیدا نشد "
            "(شاید هنوز provision نشده یا از پنل دیگری است)"
        )
    elif len(links) > 1:
        detail = f"{len(links)} لینک در متن بود — نتیجه برای اولین لینک است"
    elif q_tag and out_hits:
        top = out_hits[0]
        top_tag = (top.group_config_tag or "").strip()
        if top_tag and _norm_key(top_tag) != _norm_key(q_tag):
            detail = (
                f"نزدیک‌ترین گروه با تگ «{top_tag}» — "
                f"تگ جستجو شده «{q_tag}»"
            )

    return ConfigLookupOut(
        parsed=parsed,
        hits=out_hits,
        links_found=len(links),
        detail=detail,
    )
