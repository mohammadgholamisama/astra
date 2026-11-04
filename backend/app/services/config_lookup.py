"""Resolve a pasted share-link to Astra panel + sales group."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.http_utils import is_share_link
from app.models.astra_outbound import AstraOutbound
from app.models.group import Group
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
    normalize_host,
    normalize_match_host,
    try_normalize_endpoint,
)
from app.services.group_configs import _parse_link_meta

logger = logging.getLogger("astra.config_lookup")

_LINK_RE = re.compile(
    r"(?:vless|vmess|trojan|ss|shadowsocks|hysteria2?|hy2|tuic|wireguard)://[^\s<>\"']+",
    re.IGNORECASE,
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
    # Whole paste is a single link
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


def _parse_query(link: str) -> ConfigLookupParsed:
    proto, remark, _ = _parse_link_meta(link)
    fp = _fp_of_link(link)
    host = port = 0
    match = ""
    if fp:
        host, port, match = fp[0], fp[1], fp[2]
    preview = link if len(link) <= 120 else link[:117] + "…"
    return ConfigLookupParsed(
        link_preview=preview,
        protocol=(proto or None),
        remark=(remark or "").strip() or None,
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
    qh, qp, qm = q_fp
    ch, cp, cm = c_fp
    if qh == ch and qp == cp and (qm or "") == (cm or ""):
        return "exact"
    if uuid_match and qh == ch and qp == cp:
        return "uuid"
    if qh == ch and qp == cp and _sni_core(qm) == _sni_core(cm):
        return "sni"
    if qh == ch and qp == cp:
        return "host_port"
    return None


_QUALITY_RANK = {"exact": 0, "uuid": 1, "uri": 1, "sni": 2, "host_port": 3}


async def lookup_config(db: AsyncSession, text: str) -> ConfigLookupOut:
    links = _extract_links(text)
    if not links:
        return ConfigLookupOut(
            links_found=0,
            detail="لینک کانفیگ معتبری پیدا نشد — یک vless/vmess/… کامل بچسبانید",
        )

    # Use the first share link as the query (most common paste)
    query_link = links[0]
    parsed = _parse_query(query_link)
    q_fp = _fp_of_link(query_link)
    if not q_fp:
        return ConfigLookupOut(
            parsed=parsed,
            links_found=len(links),
            detail="نمی‌توان host/port این لینک را خواند",
        )
    q_uuid = (extract_link_client_id(query_link) or "").strip().lower()

    # Aggregate hits keyed by (panel_id, group_id)
    buckets: dict[tuple[int, int | None], dict[str, Any]] = {}

    def _add_hit(
        *,
        panel_id: int,
        panel_name: str,
        panel_type: str | None,
        panel_is_active: bool,
        group_id: int | None,
        group_name: str | None,
        quality: str,
        source_remark: str | None,
        host: str,
        port: int,
        match_host: str,
        sample_user: str | None = None,
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
                "match_quality": quality,
                "source_remark": source_remark,
                "host": host,
                "port": port,
                "match_host": match_host,
                "accounts_count": 1 if sample_user else 0,
                "sample_users": [sample_user] if sample_user else [],
            }
            return
        # Prefer better quality
        if _QUALITY_RANK.get(quality, 9) < _QUALITY_RANK.get(
            str(cur.get("match_quality") or ""), 9
        ):
            cur["match_quality"] = quality
            cur["source_remark"] = source_remark or cur.get("source_remark")
            cur["host"] = host
            cur["port"] = port
            cur["match_host"] = match_host
        if sample_user:
            cur["accounts_count"] = int(cur.get("accounts_count") or 0) + 1
            users: list[str] = list(cur.get("sample_users") or [])
            if sample_user not in users and len(users) < 5:
                users.append(sample_user)
                cur["sample_users"] = users

    # 1) Live PanelAccount share links (all users — covers every provisioned remote)
    result = await db.execute(
        select(PanelAccount)
        .where(PanelAccount.status != PanelAccountStatus.deleted)
        .options(
            selectinload(PanelAccount.panel),
            selectinload(PanelAccount.user),
        )
    )
    accounts = list(result.scalars().unique().all())

    group_ids = {
        int(a.group_id)
        for a in accounts
        if getattr(a, "group_id", None) is not None
    }
    groups_by_id: dict[int, Group] = {}
    if group_ids:
        g_rows = await db.execute(select(Group).where(Group.id.in_(sorted(group_ids))))
        groups_by_id = {int(g.id): g for g in g_rows.scalars().all()}

    for account in accounts:
        panel = account.__dict__.get("panel")
        if panel is None:
            continue
        gid = getattr(account, "group_id", None)
        group = groups_by_id.get(int(gid)) if gid is not None else None
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
            _add_hit(
                panel_id=int(panel.id),
                panel_name=str(panel.name or f"#{panel.id}"),
                panel_type=(
                    panel.type.value
                    if hasattr(panel.type, "value")
                    else str(panel.type or "")
                ),
                panel_is_active=bool(getattr(panel, "is_active", True)),
                group_id=int(gid) if gid is not None else None,
                group_name=str(getattr(group, "name", "") or "") or None,
                quality=quality,
                source_remark=(rem or "").strip() or None,
                host=c_fp[0],
                port=c_fp[1],
                match_host=c_fp[2],
                sample_user=uname or None,
            )

    # 2) Astra outbound pool — upstream URIs even before user provision
    ob_rows = await db.execute(
        select(AstraOutbound).options(selectinload(AstraOutbound.panel))
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
        # Exact URI match
        if not quality and uri.strip() == query_link.strip():
            quality = "uri"
        if not quality:
            continue
        rem = (ob.remark or "").strip() or None
        if not rem:
            _p, rem2, _ = _parse_link_meta(uri)
            rem = (rem2 or "").strip() or None
        _add_hit(
            panel_id=int(panel.id),
            panel_name=str(panel.name or f"#{panel.id}"),
            panel_type=(
                panel.type.value
                if hasattr(panel.type, "value")
                else str(panel.type or "")
            ),
            panel_is_active=bool(getattr(panel, "is_active", True)),
            group_id=None,
            group_name=None,
            quality=quality if quality != "uri" else "exact",
            source_remark=rem,
            host=c_fp[0],
            port=c_fp[1],
            match_host=c_fp[2],
        )

    hits = sorted(
        buckets.values(),
        key=lambda h: (
            _QUALITY_RANK.get(str(h.get("match_quality") or ""), 9),
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

    return ConfigLookupOut(
        parsed=parsed,
        hits=out_hits,
        links_found=len(links),
        detail=detail,
    )
