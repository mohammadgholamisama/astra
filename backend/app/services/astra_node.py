"""Local Astra node — Xray config + per-user clients + outbound pool + TCP ping."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.astra_outbound import AstraNodeClient, AstraOutbound
from app.models.panel import Panel, PanelType
from app.services.outbound_uri import (
    parse_outbound_lines,
    parse_outbound_uri,
    to_xray_outbound,
    xray_outbound_tag,
)

logger = logging.getLogger("astra.node")

DEFAULT_XRAY_DIR = Path(os.environ.get("ASTRA_XRAY_DIR", "/opt/astra/xray"))
# CF orange-cloud: Xray on loopback; nginx terminates TLS on CF HTTPS ports.
DEFAULT_LISTEN_PORT = 11020
# 443 matches Sanayi/3x-ui CF setups — Iranian clients ping this reliably.
DEFAULT_PUBLIC_PORT = 443
# VLESS+WS+TLS behind nginx/CF (Sanayi-style). XHTTP kept as optional meta.
DEFAULT_INBOUND_NETWORK = "ws"
# Reality fallback (DNS-only domains only — not used when CF proxy is on).
DEFAULT_REALITY_SNI = "www.cloudflare.com"
DEFAULT_REALITY_DEST = "www.cloudflare.com:443"
_BROKEN_REALITY_SNIS = frozenset(
    {
        "www.microsoft.com",
        "microsoft.com",
        "www.msftconnecttest.com",
        "msftconnecttest.com",
    }
)
NGINX_WS_INCLUDE = Path("/opt/astra/nginx/astra-xray-ws.conf")


def _local_xray_port(panel_id: int | None) -> int:
    """Stable loopback port per panel (nginx → Xray)."""
    base = 11000
    try:
        return base + max(0, int(panel_id or 0))
    except (TypeError, ValueError):
        return DEFAULT_LISTEN_PORT


def _ensure_ws_path(meta: dict) -> str:
    """CDN-looking WS path; stable once set."""
    path = str(meta.get("ws_path") or "").strip()
    if path.startswith("/") and len(path) >= 8:
        return path
    tok = secrets.token_urlsafe(14).replace("-", "")[:18]
    path = f"/assets/ray/{tok}"
    meta["ws_path"] = path
    return path


def panel_public_host(panel: Panel) -> str:
    meta = panel.meta or {}
    host = str(meta.get("public_host") or "").strip()
    if host:
        return host
    # base_url may be https://host or host:port
    raw = (panel.base_url or "").strip()
    raw = raw.replace("https://", "").replace("http://", "").split("/")[0]
    if ":" in raw and not raw.startswith("["):
        raw = raw.rsplit(":", 1)[0]
    return raw.strip() or "127.0.0.1"


def panel_public_port(panel: Panel) -> int:
    meta = panel.meta or {}
    try:
        return int(meta.get("public_port") or DEFAULT_PUBLIC_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PUBLIC_PORT


def panel_listen_port(panel: Panel) -> int:
    meta = panel.meta or {}
    try:
        if meta.get("listen_port") is not None:
            return int(meta["listen_port"])
        return DEFAULT_LISTEN_PORT
    except (TypeError, ValueError):
        return DEFAULT_LISTEN_PORT


def panel_listen_host(panel: Panel) -> str:
    meta = panel.meta or {}
    host = str(meta.get("listen_host") or "0.0.0.0").strip()
    return host or "0.0.0.0"


def panel_ws_path(panel: Panel) -> str:
    meta = panel.meta if isinstance(panel.meta, dict) else {}
    path = str(meta.get("ws_path") or "").strip()
    if path.startswith("/") and len(path) >= 8:
        return path
    return _ensure_ws_path(dict(meta))


def panel_xray_dir(panel: Panel) -> Path:
    meta = panel.meta or {}
    custom = str(meta.get("xray_dir") or "").strip()
    if custom:
        return Path(custom)
    return DEFAULT_XRAY_DIR / f"panel_{panel.id}"


def _detect_server_ipv4() -> str:
    """Best-effort public IPv4 of this host (for client links when domain is behind CF)."""
    for target in (("1.1.1.1", 80), ("8.8.8.8", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(target)
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except OSError:
            continue
    return ""


def _gen_reality_keys() -> tuple[str, str]:
    """Return (private_key, public_key) via `xray x25519`. Raises if unavailable."""
    import subprocess

    xray_bin = os.environ.get("ASTRA_XRAY_BIN", "/usr/local/bin/xray")
    if not Path(xray_bin).is_file():
        raise RuntimeError(f"xray binary missing: {xray_bin}")
    out = subprocess.check_output([xray_bin, "x25519"], text=True, timeout=10)
    priv = pub = ""
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key_l = key.strip().lower()
        val = val.strip()
        if key_l == "privatekey" or key_l.startswith("private"):
            priv = val
        elif "public" in key_l:  # "Password (PublicKey)" on Xray 26+
            pub = val
    if not (priv and pub):
        raise RuntimeError(f"xray x25519 parse failed: {out!r}")
    return priv, pub


def ensure_reality_keys(meta: dict) -> dict:
    m = dict(meta)
    priv = str(m.get("reality_private_key") or "").strip()
    pub = str(m.get("reality_public_key") or "").strip()
    if not (priv and pub):
        priv, pub = _gen_reality_keys()
        m["reality_private_key"] = priv
        m["reality_public_key"] = pub
    if not str(m.get("reality_short_id") or "").strip():
        m["reality_short_id"] = secrets.token_hex(8)
    sni = str(m.get("reality_sni") or "").strip().lower()
    dest = str(m.get("reality_dest") or "").strip().lower()
    # Migrate away from dests whose TLS Certificate record breaks Reality (>8192B).
    if (not sni) or sni in _BROKEN_REALITY_SNIS or any(
        bad in dest for bad in ("microsoft.com", "msftconnecttest.com")
    ):
        m["reality_sni"] = DEFAULT_REALITY_SNI
        m["reality_dest"] = DEFAULT_REALITY_DEST
    else:
        m.setdefault("reality_sni", DEFAULT_REALITY_SNI)
        m.setdefault("reality_dest", DEFAULT_REALITY_DEST)
    return m


def panel_inbound_network(panel: Panel | dict | None = None, meta: dict | None = None) -> str:
    if meta is None:
        if isinstance(panel, Panel):
            meta = panel.meta if isinstance(panel.meta, dict) else {}
        elif isinstance(panel, dict):
            meta = panel
        else:
            meta = {}
    net = str((meta or {}).get("inbound_network") or DEFAULT_INBOUND_NETWORK).strip().lower()
    if net in ("splithttp", "xhttp"):
        return "xhttp"
    if net in ("ws", "websocket"):
        return "ws"
    return net if net in ("ws", "xhttp", "reality") else DEFAULT_INBOUND_NETWORK


def panel_client_host(panel: Panel) -> str:
    """Host clients dial — always the public domain for WS/XHTTP+CF; Reality may fall back to IP."""
    meta = panel.meta or {}
    forced = str(meta.get("client_host") or "").strip()
    if forced:
        return forced
    host = str(meta.get("public_host") or "").strip()
    ip = str(meta.get("public_ip") or "").strip() or _detect_server_ipv4()
    net = panel_inbound_network(meta=meta)
    if net in ("ws", "xhttp"):
        # Cloudflare orange-cloud: clients MUST dial the domain (not origin IP).
        return host or panel_public_host(panel) or ip or "127.0.0.1"
    if host:
        ok, _note = public_host_dns_ok(host, allow_cloudflare=False)
        if ok:
            return host
        if ip:
            return ip
        return host
    if ip:
        return ip
    return panel_public_host(panel)


# Cloudflare anycast — if public_host resolves here, Reality via that name will fail.
_CF_IPV4_PREFIXES = (
    "103.21.244.", "103.22.200.", "103.31.4.", "104.16.", "104.17.", "104.18.",
    "104.19.", "104.20.", "104.21.", "104.22.", "104.23.", "104.24.", "104.25.",
    "104.26.", "104.27.", "104.28.", "108.162.", "131.0.72.", "141.101.",
    "162.158.", "172.64.", "172.65.", "172.66.", "172.67.", "173.245.",
    "188.114.", "190.93.", "197.234.", "198.41.",
)


def _is_cloudflare_ipv4(ip: str) -> bool:
    return any(ip.startswith(p) for p in _CF_IPV4_PREFIXES)


def resolve_host_ipv4(host: str) -> list[str]:
    host = (host or "").strip()
    if not host or host.replace(".", "").isdigit():
        return [host] if host else []
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip and ip not in out:
            out.append(ip)
    return out


def public_host_dns_ok(
    host: str, *, allow_cloudflare: bool = True
) -> tuple[bool, str]:
    """DNS check for client host. XHTTP allows Cloudflare proxy; Reality does not."""
    host = (host or "").strip()
    if not host:
        return False, "هاست عمومی خالی است"
    if host.replace(".", "").isdigit():
        return True, "IP مستقیم"
    ips = resolve_host_ipv4(host)
    if not ips:
        return False, f"DNS برای {host} resolve نشد"
    cf = [ip for ip in ips if _is_cloudflare_ipv4(ip)]
    if cf:
        if allow_cloudflare:
            return True, f"Cloudflare proxy → {', '.join(cf[:2])} (CDN)"
        return False, (
            f"{host} پشت Cloudflare پروکسی است ({', '.join(cf[:2])}). "
            "برای Reality باید DNS Only باشد."
        )
    return True, f"DNS مستقیم → {', '.join(ips[:3])}"


def ensure_panel_token(meta: dict | None, *, panel_id: int | None = None) -> dict:
    """Defaults: VLESS + WS + TLS behind nginx/Cloudflare (Sanayi-style).

    Xray listens on 127.0.0.1; clients dial public_host:public_port (from UI).
    One inbound serves every outbound share-link.
    """
    m = dict(meta or {})
    if not str(m.get("node_token") or "").strip():
        m["node_token"] = secrets.token_urlsafe(32)
    # Keep explicit operator choice; default to WS for Iranian client ping/CF.
    raw_net = str(m.get("inbound_network") or "").strip().lower()
    if raw_net in ("reality", "xhttp", "ws", "websocket", "splithttp"):
        m["inbound_network"] = "xhttp" if raw_net in ("xhttp", "splithttp") else (
            "ws" if raw_net in ("ws", "websocket") else raw_net
        )
    else:
        m["inbound_network"] = DEFAULT_INBOUND_NETWORK
    m["tls_terminate"] = "nginx"
    m["listen_host"] = "127.0.0.1"
    m["listen_port"] = _local_xray_port(panel_id)
    try:
        pub = int(m["public_port"]) if m.get("public_port") is not None else None
    except (TypeError, ValueError):
        pub = None
    if pub is None or pub < 1 or pub > 65535:
        pub = DEFAULT_PUBLIC_PORT
    m["public_port"] = int(pub)
    _ensure_ws_path(m)
    # Keep Reality keys around in case operator switches later (unused in WS mode).
    try:
        m = ensure_reality_keys(m)
    except Exception:  # noqa: BLE001
        pass
    if not str(m.get("public_ip") or "").strip():
        m["public_ip"] = _detect_server_ipv4()
    return m


async def sync_nginx_ws_upstreams(db: AsyncSession) -> Path | None:
    """Write nginx include: CDN path → local Xray (WS or XHTTP)."""
    result = await db.execute(select(Panel).where(Panel.type == PanelType.astra))
    panels = list(result.scalars().all())
    lines = [
        "# Auto-generated by Astra — VLESS CDN upstreams (WS/XHTTP)",
        "# Do not edit; rewritten on node rebuild.",
        "",
    ]
    for p in panels:
        meta = ensure_panel_token(p.meta if isinstance(p.meta, dict) else {}, panel_id=p.id)
        p.meta = meta
        net = panel_inbound_network(meta=meta)
        if net == "reality":
            continue
        path = str(meta.get("ws_path") or panel_ws_path(p)).rstrip("/") or "/assets/ray/x"
        port = int(meta.get("listen_port") or panel_listen_port(p))
        lines.append(f"location ^~ {path} {{")
        if net == "xhttp":
            # XHTTP needs h2c to Xray (XTLS: use grpc_pass behind nginx/CF).
            lines.append("    client_max_body_size 0;")
            lines.append("    client_body_timeout 5m;")
            lines.append("    grpc_read_timeout 86400s;")
            lines.append("    grpc_send_timeout 86400s;")
            lines.append("    grpc_set_header Host $host;")
            lines.append("    grpc_set_header X-Real-IP $remote_addr;")
            lines.append("    grpc_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
            lines.append("    grpc_set_header X-Forwarded-Proto $scheme;")
            lines.append("    grpc_set_header CF-Connecting-IP $http_cf_connecting_ip;")
            lines.append(f"    grpc_pass grpc://127.0.0.1:{port};")
        else:
            # Sanayi-style VLESS+WS+TLS — low-latency, CF orange/grey friendly.
            lines.append("    proxy_redirect off;")
            lines.append("    proxy_buffering off;")
            lines.append("    proxy_request_buffering off;")
            lines.append("    tcp_nodelay on;")
            lines.append("    proxy_socket_keepalive on;")
            lines.append(f"    proxy_pass http://127.0.0.1:{port};")
            lines.append("    proxy_http_version 1.1;")
            lines.append("    proxy_set_header Upgrade $http_upgrade;")
            lines.append('    proxy_set_header Connection "upgrade";')
            lines.append("    proxy_set_header Host $host;")
            lines.append("    proxy_set_header X-Real-IP $remote_addr;")
            lines.append("    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
            lines.append("    proxy_set_header X-Forwarded-Proto $scheme;")
            lines.append("    proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;")
            lines.append("    proxy_read_timeout 86400s;")
            lines.append("    proxy_send_timeout 86400s;")
            lines.append("    proxy_connect_timeout 4s;")
        lines.append("}")
        lines.append("")
    try:
        NGINX_WS_INCLUDE.parent.mkdir(parents=True, exist_ok=True)
        # Ensure include exists even with zero panels (nginx include must not 404)
        if not panels:
            lines.append("# (no Astra panels yet)")
            lines.append("")
        NGINX_WS_INCLUDE.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("nginx ws include write failed: %s", exc)
        return None
    await db.flush()
    # Best-effort reload (astra user → sudo astra-helper)
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "/usr/local/bin/astra-helper",
            "reload-nginx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=25)
    except Exception as exc:  # noqa: BLE001
        logger.debug("nginx reload via helper: %s", exc)
    return NGINX_WS_INCLUDE


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def protocol_ping(row: AstraOutbound, *, timeout: float = 10.0) -> tuple[bool, int | None]:
    """Real protocol ping via short-lived Xray (Reality/TLS/WS/…), not bare TCP.

    Spins a local SOCKS inbound → this outbound → HTTP 204 probe.
    Reality that only opens TCP to a decoy host will correctly fail here.
    """
    host = (row.host or "").strip()
    port = int(row.port or 0)
    if not host or port <= 0:
        return False, None

    xray_bin = os.environ.get("ASTRA_XRAY_BIN", "/usr/local/bin/xray")
    if not Path(xray_bin).is_file():
        # Dev fallback — still better than lying about Reality
        return await _tcp_fallback(host, port, timeout=min(timeout, 3.0))

    raw = dict(row.parsed or {})
    if "host" not in raw:
        raw["host"] = host
    if "port" not in raw:
        raw["port"] = port
    if (row.protocol or "").lower() == "vless" and "user" not in raw:
        try:
            p = parse_outbound_uri(row.uri)
            raw = p.raw
        except Exception:
            pass

    try:
        xo = to_xray_outbound(int(row.id or 1), raw, row.protocol or "vless")
    except Exception as exc:  # noqa: BLE001
        logger.debug("protocol_ping build outbound failed: %s", exc)
        return False, None

    tag = str(xo.get("tag") or "ob-test")
    socks_port = _free_tcp_port()
    cfg = {
        "log": {"loglevel": "error"},
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "socks",
                "settings": {"udp": False, "auth": "noauth"},
            }
        ],
        "outbounds": [
            xo,
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["socks-in"], "outboundTag": tag},
            ],
        },
    }

    tmp_dir = Path(os.environ.get("ASTRA_XRAY_DIR", "/opt/astra/xray")) / "ping"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmp_dir = Path("/tmp")
    cfg_path = tmp_dir / f"ping-{row.id or 0}-{socks_port}.json"
    proc: asyncio.subprocess.Process | None = None
    try:
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            xray_bin,
            "run",
            "-config",
            str(cfg_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # wait until socks accepts
        ready = False
        for _ in range(25):
            await asyncio.sleep(0.08)
            if proc.returncode is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", socks_port), timeout=0.2):
                    ready = True
                    break
            except OSError:
                continue
        if not ready:
            return False, None

        # Probe through the tunnel — Reality must complete handshake
        probe_urls = (
            "http://www.gstatic.com/generate_204",
            "http://cp.cloudflare.com/",
            "http://connectivitycheck.gstatic.com/generate_204",
        )
        t0 = time.perf_counter()
        ok = False
        for url in probe_urls:
            try:
                curl = await asyncio.create_subprocess_exec(
                    "curl",
                    "-sS",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--connect-timeout",
                    "6",
                    "--max-time",
                    str(max(4, int(timeout) - 1)),
                    "-x",
                    f"socks5h://127.0.0.1:{socks_port}",
                    url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out_b, _ = await asyncio.wait_for(curl.communicate(), timeout=timeout)
                code = (out_b or b"").decode("utf-8", "replace").strip()
                if curl.returncode == 0 and code and code[0] in "123":
                    ok = True
                    break
            except Exception:
                continue
        ms = int((time.perf_counter() - t0) * 1000) if ok else None
        return ok, ms
    except Exception as exc:  # noqa: BLE001
        logger.debug("protocol_ping failed id=%s: %s", row.id, exc)
        return False, None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass
        try:
            cfg_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _tcp_fallback(
    host: str, port: int, timeout: float = 2.5
) -> tuple[bool, int | None]:
    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, int((time.perf_counter() - t0) * 1000)
    except Exception:
        return False, None


async def ping_outbound(db: AsyncSession, row: AstraOutbound) -> AstraOutbound:
    ok, ms = await protocol_ping(row)
    row.last_ping_ok = ok
    row.last_ping_ms = ms
    row.last_ping_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def ping_outbounds_batch(
    db: AsyncSession, rows: list[AstraOutbound], *, concurrency: int = 4
) -> list[AstraOutbound]:
    """Ping many outbounds with limited parallel Xray probes."""
    sem = asyncio.Semaphore(max(1, min(8, int(concurrency))))

    async def _one(r: AstraOutbound) -> AstraOutbound:
        async with sem:
            return await ping_outbound(db, r)

    if not rows:
        return []
    return list(await asyncio.gather(*[_one(r) for r in rows]))


async def reconcile_outbounds_health(
    db: AsyncSession,
    panel: Panel,
    *,
    concurrency: int = 4,
    force_refresh: bool = False,
) -> dict[str, int]:
    """Re-ping outbounds, rebuild node if health changed, refresh sub links.

    When an outbound was down (removed from sub) and comes back, this puts its
    share-link back into PanelAccount.links / live sub generation.
    """
    rows = await list_outbounds(db, panel.id)
    if not rows:
        return {"pinged": 0, "changed": 0, "ok": 0}
    before = {int(r.id): r.last_ping_ok for r in rows}
    await ping_outbounds_batch(db, rows, concurrency=concurrency)
    changed = 0
    ok_n = 0
    for r in rows:
        key = int(r.id)
        prev = before.get(key)
        if prev != r.last_ping_ok:
            changed += 1
        if r.is_enabled and r.last_ping_ok is True:
            ok_n += 1
    if changed or force_refresh:
        await rebuild_xray_config(db, panel)
        await refresh_panel_account_links(db, panel)
    return {"pinged": len(rows), "changed": changed, "ok": ok_n}


def outbound_included_in_sub(ob: AstraOutbound) -> bool:
    """Whether this outbound should appear in client share-links / sub / balancer.

    Disabled → never. Explicit ping-fail → hide until the next successful ping
    (API or background reconcile_outbounds_health) brings it back.
    """
    if not ob.is_enabled:
        return False
    return ob.last_ping_ok is not False


async def add_outbounds_from_text(
    db: AsyncSession, panel: Panel, text: str
) -> tuple[list[AstraOutbound], list[str]]:
    """Astra panels accept exactly one outbound (CDN/edge hop)."""
    created: list[AstraOutbound] = []
    errors: list[str] = []
    try:
        parsed_list = parse_outbound_lines(text)
    except ValueError as exc:
        return [], [str(exc)]

    existing_rows = await list_outbounds(db, panel.id)
    if existing_rows:
        return [], [
            "در هر پنل Astra فقط یک اوتباند مجاز است. ابتدا اوتباند فعلی را حذف کنید."
        ]

    if not parsed_list:
        return [], ["لینک معتبری پیدا نشد"]

    if len(parsed_list) > 1:
        errors.append("فقط اولین لینک اضافه شد — هر پنل Astra یک اوتباند دارد")
        parsed_list = parsed_list[:1]

    p = parsed_list[0]
    row = AstraOutbound(
        panel_id=panel.id,
        uri=p.uri,
        uri_hash=p.uri_hash,
        remark=p.remark,
        protocol=p.protocol,
        host=p.host,
        port=p.port,
        parsed=p.raw,
        is_enabled=True,
        sort_order=1,
    )
    db.add(row)
    created.append(row)
    await db.flush()
    try:
        await ping_outbound(db, row)
    except Exception:  # noqa: BLE001
        pass
    await rebuild_xray_config(db, panel)
    try:
        await refresh_panel_account_links(db, panel)
    except Exception:  # noqa: BLE001
        logger.debug("refresh links after outbound add failed", exc_info=True)
    return created, errors


async def list_outbounds(db: AsyncSession, panel_id: int) -> list[AstraOutbound]:
    result = await db.execute(
        select(AstraOutbound)
        .where(AstraOutbound.panel_id == panel_id)
        .order_by(AstraOutbound.sort_order, AstraOutbound.id)
    )
    return list(result.scalars().all())


async def delete_outbound(db: AsyncSession, panel: Panel, outbound_id: int) -> bool:
    row = await db.get(AstraOutbound, outbound_id)
    if not row or row.panel_id != panel.id:
        return False
    await db.delete(row)
    await db.flush()
    await rebuild_xray_config(db, panel)
    try:
        await refresh_panel_account_links(db, panel)
    except Exception:  # noqa: BLE001
        logger.debug("refresh links after outbound delete failed", exc_info=True)
    return True


async def set_outbound_enabled(
    db: AsyncSession, panel: Panel, outbound_id: int, enabled: bool
) -> AstraOutbound | None:
    row = await db.get(AstraOutbound, outbound_id)
    if not row or row.panel_id != panel.id:
        return None
    row.is_enabled = bool(enabled)
    await db.flush()
    await rebuild_xray_config(db, panel)
    try:
        await refresh_panel_account_links(db, panel)
    except Exception:  # noqa: BLE001
        logger.debug("refresh links after outbound toggle failed", exc_info=True)
    return row


async def share_links_by_email(db: AsyncSession, panel: Panel) -> dict[str, list[str]]:
    """Current VLESS share links for every client on this panel (host/port from panel meta)."""
    outbounds = await list_outbounds(db, panel.id)
    clients = await list_clients(db, panel.id)
    return {
        c.email: (_client_links(panel, c, outbounds) if c.enable else [])
        for c in clients
    }


async def refresh_panel_account_links(db: AsyncSession, panel: Panel) -> int:
    """Rewrite PanelAccount.links after public_host / port / outbound changes."""
    from app.models.user import PanelAccount

    by_email = await share_links_by_email(db, panel)
    accounts = (
        await db.execute(
            select(PanelAccount).where(PanelAccount.panel_id == panel.id)
        )
    ).scalars().all()
    updated = 0
    for acc in accounts:
        email = (acc.remote_username or "").strip()
        if not email:
            continue
        fresh = list(by_email.get(email) or [])
        prev = [str(x) for x in (acc.links or []) if x]
        if prev != fresh:
            acc.links = fresh
            acc.links_by_group = None
            updated += 1
    if updated:
        await db.flush()
    return updated


def _client_links(
    panel: Panel,
    client: AstraNodeClient,
    outbounds: list[AstraOutbound],
) -> list[str]:
    """One VLESS share-link per healthy outbound (same UUID; remark = outbound)."""
    host = panel_client_host(panel)
    port = panel_public_port(panel)
    meta = panel.meta if isinstance(panel.meta, dict) else {}
    net = panel_inbound_network(meta=meta)
    links: list[str] = []
    for ob in outbounds:
        if not outbound_included_in_sub(ob):
            continue
        remark = (ob.remark or f"ob-{ob.id}").replace(" ", "_")
        if net == "ws":
            # VLESS + WS + TLS. ALPN must be http/1.1 only — h2 breaks
            # delay/ping tests in most clients and is unreliable over WS.
            path = panel_ws_path(panel)
            q = {
                "encryption": "none",
                "security": "tls",
                "type": "ws",
                "host": host,
                "path": path,
                "sni": host,
                "fp": "chrome",
                "alpn": "http/1.1",
            }
        elif net == "xhttp":
            path = panel_ws_path(panel)
            q = {
                "encryption": "none",
                "security": "tls",
                "type": "xhttp",
                "host": host,
                "path": path,
                "mode": "packet-up",
                "sni": host,
                "fp": "chrome",
                "alpn": "http/1.1",
            }
        else:
            pbk = str(meta.get("reality_public_key") or "").strip()
            sid = str(meta.get("reality_short_id") or "").strip()
            sni = str(meta.get("reality_sni") or DEFAULT_REALITY_SNI).strip()
            q = {
                "encryption": "none",
                "security": "reality",
                "type": "tcp",
                "headerType": "none",
                "flow": "xtls-rprx-vision",
                "sni": sni,
                "fp": "chrome",
                "pbk": pbk,
                "sid": sid,
                "spx": "/",
            }
        link = (
            f"vless://{client.uuid}@{host}:{port}?{urlencode(q)}"
            f"#{quote(remark)}"
        )
        links.append(link)
    return links


async def upsert_client(
    db: AsyncSession,
    panel: Panel,
    *,
    email: str,
    uuid: str | None = None,
    data_limit: int = 0,
    expire: int = 0,
    enable: bool = True,
    limit_ip: int = 0,
    rebuild: bool = True,
    sync_nginx: bool = True,
) -> AstraNodeClient:
    email = (email or "").strip()
    result = await db.execute(
        select(AstraNodeClient).where(
            AstraNodeClient.panel_id == panel.id,
            AstraNodeClient.email == email,
        )
    )
    row = result.scalar_one_or_none()
    # Xray config only embeds uuid/enable — data_limit/expire live in DB only.
    # Skip expensive xray/nginx reload when quota fields alone change.
    needs_xray = False
    if row is None:
        row = AstraNodeClient(
            panel_id=panel.id,
            email=email,
            uuid=uuid or str(uuid_lib.uuid4()),
            enable=enable,
            data_limit=int(data_limit or 0),
            expire=int(expire or 0),
            limit_ip=int(limit_ip or 0),
        )
        db.add(row)
        needs_xray = True
    else:
        if uuid and str(uuid) != str(row.uuid or ""):
            row.uuid = uuid
            needs_xray = True
        if bool(row.enable) != bool(enable):
            needs_xray = True
        row.enable = bool(enable)
        row.data_limit = int(data_limit or 0)
        row.expire = int(expire or 0)
        row.limit_ip = int(limit_ip or 0)
    await db.flush()
    if rebuild and needs_xray:
        await rebuild_xray_config(db, panel, sync_nginx=sync_nginx)
    return row


async def get_client(
    db: AsyncSession, panel_id: int, email: str
) -> AstraNodeClient | None:
    result = await db.execute(
        select(AstraNodeClient).where(
            AstraNodeClient.panel_id == panel_id,
            AstraNodeClient.email == email,
        )
    )
    return result.scalar_one_or_none()


async def delete_client(db: AsyncSession, panel: Panel, email: str) -> bool:
    row = await get_client(db, panel.id, email)
    if not row:
        return False
    await db.delete(row)
    await db.flush()
    await rebuild_xray_config(db, panel)
    return True


async def client_usage_and_links(
    db: AsyncSession, panel: Panel, email: str
) -> tuple[AstraNodeClient | None, list[str], int]:
    row = await get_client(db, panel.id, email)
    if not row:
        return None, [], 0
    # Soft-enforce local quota on enable flag
    used = int(row.used_up or 0) + int(row.used_down or 0)
    if row.enable and int(row.data_limit or 0) > 0 and used >= int(row.data_limit):
        row.enable = False
        await db.flush()
        await rebuild_xray_config(db, panel)
    if row.expire and int(row.expire) > 0 and int(time.time()) >= int(row.expire):
        if row.enable:
            row.enable = False
            await db.flush()
            await rebuild_xray_config(db, panel)
    outbounds = await list_outbounds(db, panel.id)
    links = _client_links(panel, row, outbounds) if row.enable else []
    return row, links, used


async def rebuild_xray_config(
    db: AsyncSession, panel: Panel, *, sync_nginx: bool = True
) -> Path | None:
    """Write Xray config for this panel and best-effort reload service.

    ``sync_nginx=False`` skips rewriting/reloading the shared WS include — use
    when updating many Astra panels in a batch, then call
    ``sync_nginx_ws_upstreams`` once at the end.
    """
    # Migrate meta → WS+TLS defaults before building
    meta = ensure_panel_token(
        panel.meta if isinstance(panel.meta, dict) else {}, panel_id=panel.id
    )
    if not meta.get("public_host"):
        meta["public_host"] = panel_public_host(panel)
    panel.meta = meta
    await db.flush()

    outbounds = await list_outbounds(db, panel.id)
    clients = (
        await db.execute(
            select(AstraNodeClient).where(AstraNodeClient.panel_id == panel.id)
        )
    ).scalars().all()

    listen = panel_listen_port(panel)
    listen_host = panel_listen_host(panel)
    xdir = panel_xray_dir(panel)
    try:
        xdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("xray dir %s: %s", xdir, exc)
        return None

    net = panel_inbound_network(meta=meta)
    inbound_clients = []
    for c in clients:
        if not c.enable:
            continue
        entry: dict[str, Any] = {
            "id": c.uuid,
            "email": c.email,
            "level": 0,
        }
        # Vision is Reality/TCP-only — never set on XHTTP.
        if net == "reality":
            entry["flow"] = "xtls-rprx-vision"
        inbound_clients.append(entry)

    xray_outbounds: list[dict[str, Any]] = [
        {"tag": "direct", "protocol": "freedom", "settings": {}},
        {"tag": "block", "protocol": "blackhole", "settings": {}},
    ]
    balancer_selectors: list[str] = []
    for ob in outbounds:
        if not outbound_included_in_sub(ob):
            continue
        try:
            raw = dict(ob.parsed or {})
            if "host" not in raw:
                raw["host"] = ob.host
            if "port" not in raw:
                raw["port"] = ob.port
            if ob.protocol == "vless" and "user" not in raw:
                # from uri parse
                try:
                    p = parse_outbound_uri(ob.uri)
                    raw = p.raw
                except Exception:
                    pass
            xo = to_xray_outbound(ob.id, raw, ob.protocol)
            xray_outbounds.append(xo)
            balancer_selectors.append(xray_outbound_tag(ob.id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip outbound %s: %s", ob.id, exc)

    if not balancer_selectors:
        # No healthy upstream — still listen but blackhole
        routing_rules: list[dict[str, Any]] = [
            {"type": "field", "inboundTag": ["astra-in"], "outboundTag": "block"}
        ]
        balancers: list[dict[str, Any]] = []
    else:
        balancers = [
            {
                "tag": "ob-balance",
                "selector": balancer_selectors,
                "strategy": {"type": "random"},
            }
        ]
        routing_rules = [
            {
                "type": "field",
                "inboundTag": ["astra-in"],
                "balancerTag": "ob-balance",
            }
        ]

    # tcpNoDelay: lower ping jitter on WS; widely supported in Xray sockopt
    _sockopt = {"tcpNoDelay": True}
    if net == "ws":
        cdn_path = _ensure_ws_path(meta)
        panel.meta = meta
        stream = {
            "network": "ws",
            "security": "none",
            "wsSettings": {
                "path": cdn_path,
                "host": str(meta.get("public_host") or panel_public_host(panel) or ""),
            },
            "sockopt": _sockopt,
        }
    elif net == "xhttp":
        # TLS at Cloudflare/nginx — Xray sees plain HTTP/XHTTP on loopback.
        cdn_path = _ensure_ws_path(meta)
        panel.meta = meta
        stream = {
            "network": "xhttp",
            "security": "none",
            "xhttpSettings": {
                "path": cdn_path,
                "mode": "auto",
                "noSSEHeader": True,  # better CF buffering behavior
            },
            "sockopt": _sockopt,
        }
    else:
        priv = str(meta.get("reality_private_key") or "")
        sid = str(meta.get("reality_short_id") or "")
        sni = str(meta.get("reality_sni") or DEFAULT_REALITY_SNI)
        dest = str(meta.get("reality_dest") or DEFAULT_REALITY_DEST)
        stream = {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "target": dest,
                "dest": dest,
                "xver": 0,
                "serverNames": [sni],
                "privateKey": priv,
                "shortIds": [sid, ""],
            },
        }

    config: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "stats": {},
        "api": {
            "tag": "api",
            "services": ["HandlerService", "StatsService", "LoggerService"],
        },
        "policy": {
            # bufferSize (KB): higher = better throughput under many users;
            # connIdle: drop dead sockets so FDs recycle (helps ping jitter).
            "levels": {
                "0": {
                    "handshake": 8,
                    "connIdle": 300,
                    "uplinkOnly": 2,
                    "downlinkOnly": 5,
                    "bufferSize": 128,
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            },
        },
        "inbounds": [
            {
                # tag MUST be "api" for StatsService / xray api CLI
                "tag": "api",
                "listen": "127.0.0.1",
                "port": int((panel.meta or {}).get("xray_api_port") or (10085 + int(panel.id))),
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": "astra-in",
                "listen": listen_host,
                "port": listen,
                "protocol": "vless",
                "settings": {
                    "clients": inbound_clients,
                    "decryption": "none",
                },
                "streamSettings": stream,
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            },
        ],
        "outbounds": xray_outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": balancers,
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                *routing_rules,
            ],
        },
    }
    # Xray requires api outbound
    config["outbounds"].insert(
        0, {"tag": "api", "protocol": "freedom", "settings": {}}
    )

    cfg_path = xdir / "config.json"
    try:
        cfg_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("write xray config failed: %s", exc)
        return None

    await _reload_xray(panel, xdir, cfg_path)
    if sync_nginx:
        try:
            await sync_nginx_ws_upstreams(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("nginx ws sync: %s", exc)
    return cfg_path


async def _reload_xray(panel: Panel, xdir: Path, cfg_path: Path) -> None:
    """Restart unit astra-xray@{panel_id} via astra-helper (sudo) or systemctl."""
    helper = "/usr/local/bin/astra-helper"
    cmds = [
        ["sudo", helper, "xray-restart", str(panel.id)],
        [helper, "xray-restart", str(panel.id)],
        ["sudo", "systemctl", "enable", "--now", f"astra-xray@{panel.id}"],
        ["systemctl", "restart", f"astra-xray@{panel.id}"],
    ]
    try:
        for cmd in cmds:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
                if proc.returncode == 0:
                    logger.info("xray reload ok via %s", " ".join(cmd))
                    # brief wait so listen_open health checks pass
                    await asyncio.sleep(0.25)
                    return
                logger.warning(
                    "xray reload %s → rc=%s %s",
                    cmd,
                    proc.returncode,
                    ((err or out or b"")[-300:]).decode("utf-8", "replace"),
                )
            except FileNotFoundError:
                continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("xray reload failed: %s", exc)

    marker = xdir / "NEED_RELOAD"
    try:
        marker.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    _ = cfg_path


def _parse_xray_user_traffic(text: str) -> dict[str, int]:
    """Parse `xray api statsquery` output → email → uplink+downlink bytes.

    Xray 26+ emits JSON with name/value on separate lines; older builds used
    single-line `user>>>email>>>traffic>>>uplink: N`.
    """
    totals: dict[str, int] = {}

    def _add(email: str, n: int) -> None:
        email = (email or "").strip()
        if not email or n < 0:
            return
        totals[email] = totals.get(email, 0) + int(n)

    raw = (text or "").strip()
    if not raw:
        return totals

    # Preferred: JSON envelope from modern xray api
    try:
        payload = json.loads(raw)
        stats = payload.get("stat") if isinstance(payload, dict) else None
        if stats is None and isinstance(payload, list):
            stats = payload
        if isinstance(stats, dict):
            stats = [stats]
        if isinstance(stats, list):
            for item in stats:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if "user>>>" not in name or "traffic>>>" not in name:
                    continue
                parts = name.split(">>>")
                if len(parts) < 4:
                    continue
                email = parts[1]
                try:
                    val = int(item.get("value") or 0)
                except (TypeError, ValueError):
                    val = 0
                _add(email, val)
            if totals:
                return totals
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Fallback: line-oriented / protobuf-ish text
    for line in raw.splitlines():
        line = line.strip()
        if "user>>>" not in line or "traffic>>>" not in line:
            continue
        try:
            m = re.search(
                r'user>>>([^>\s"]+)>>>traffic>>>(?:uplink|downlink)["\']?\s*(?::|,|\s+value:)\s*(\d+)',
                line,
                re.I,
            )
            if m:
                _add(m.group(1), int(m.group(2)))
                continue
            m2 = re.search(
                r'name["\']?\s*[:=]\s*["\']?user>>>([^>"\s]+)>>>traffic>>>(?:uplink|downlink)',
                line,
                re.I,
            )
            m3 = re.search(r'value["\']?\s*[:=]\s*(\d+)', line, re.I)
            if m2 and m3:
                _add(m2.group(1), int(m3.group(1)))
                continue
            left, _, num_s = line.partition(":")
            num = int(re.sub(r"[^\d]", "", num_s) or 0)
            parts = left.replace('"', "").replace("'", "").split(">>>")
            if len(parts) >= 4 and parts[0].endswith("user"):
                _add(parts[1], num)
        except Exception:
            continue
    return totals


def _apply_xray_counter_delta(client: AstraNodeClient, xray_total: int) -> int:
    """Apply StatsService counter → cumulative used_down.

    Xray counters reset on restart. We keep ``stats_counter`` as the last seen
    live reading and only ADD the positive delta to cumulative usage.

    Returns bytes added this call (0 if primed/no change).
    """
    xray_total = max(0, int(xray_total or 0))
    prev = client.stats_counter
    if prev is None:
        # First sync after upgrade / new client: adopt baseline, keep cumulative.
        client.stats_counter = xray_total
        return 0
    prev_i = max(0, int(prev))
    if xray_total >= prev_i:
        delta = xray_total - prev_i
    else:
        # Counter went backwards → Xray restarted; credit the new reading fully.
        delta = xray_total
    client.stats_counter = xray_total
    if delta <= 0:
        return 0
    base = int(client.used_down or 0) + int(client.used_up or 0)
    client.used_down = base + delta
    client.used_up = 0
    return delta


async def _push_client_usage_to_accounts(
    db: AsyncSession, panel_id: int, by_email: dict[str, int]
) -> int:
    """Mirror cumulative Astra client usage onto PanelAccount.used_traffic.

    Exact mirror (including shrink to 0 after reset). AstraNodeClient is the
    source of truth — never-shrink here made traffic resets look like no-ops.
    """
    if not by_email:
        return 0
    from app.models.user import PanelAccount

    accounts = (
        await db.execute(
            select(PanelAccount).where(PanelAccount.panel_id == panel_id)
        )
    ).scalars().all()
    n = 0
    for acc in accounts:
        email = (acc.remote_username or "").strip()
        if not email or email not in by_email:
            continue
        got = int(by_email[email])
        prev = int(acc.used_traffic or 0)
        if got != prev:
            acc.used_traffic = got
            n += 1
    return n


async def sync_traffic_from_xray(db: AsyncSession, panel: Panel) -> int:
    """Pull per-email uplink+downlink from Xray StatsService and accumulate usage."""
    api_port = int((panel.meta or {}).get("xray_api_port") or (10085 + int(panel.id)))
    xray_bin = os.environ.get("ASTRA_XRAY_BIN", "/usr/local/bin/xray")
    if not Path(xray_bin).is_file():
        return 0
    try:
        proc = await asyncio.create_subprocess_exec(
            xray_bin,
            "api",
            "statsquery",
            f"--server=127.0.0.1:{api_port}",
            "-pattern",
            "user>>>",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=8)
        if proc.returncode != 0 or not out_b:
            if err_b:
                logger.debug(
                    "xray statsquery rc=%s err=%s",
                    proc.returncode,
                    err_b.decode("utf-8", "replace")[:300],
                )
            return 0
        text = out_b.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        logger.debug("xray statsquery failed: %s", exc)
        return 0

    totals = _parse_xray_user_traffic(text)
    updated = 0
    need_rebuild = False
    clients = (
        await db.execute(
            select(AstraNodeClient).where(AstraNodeClient.panel_id == panel.id)
        )
    ).scalars().all()
    by_email: dict[str, int] = {}
    for c in clients:
        if c.email in totals:
            added = _apply_xray_counter_delta(c, int(totals[c.email]))
            if added:
                updated += 1
                logger.debug(
                    "astra traffic panel=%s email=%s +%s → %s (counter=%s)",
                    panel.id,
                    c.email,
                    added,
                    int(c.used_down or 0),
                    c.stats_counter,
                )
        used = int(c.used_down or 0) + int(c.used_up or 0)
        by_email[c.email] = used
        if int(c.data_limit or 0) > 0 and used >= int(c.data_limit) and c.enable:
            c.enable = False
            need_rebuild = True
            updated += 1
    # Mirror cumulatives → PanelAccount so «مصرف پنل‌ها» stays accurate
    try:
        await _push_client_usage_to_accounts(db, panel.id, by_email)
    except Exception:  # noqa: BLE001
        logger.debug("push client usage to accounts failed", exc_info=True)
    await db.flush()
    if need_rebuild:
        await rebuild_xray_config(db, panel)
    return updated


async def apply_usage_delta(
    db: AsyncSession, panel_id: int, email: str, used_total: int
) -> AstraNodeClient | None:
    """Set absolute used traffic (up+down combined into used_down)."""
    row = await get_client(db, panel_id, email)
    if not row:
        return None
    used_total = max(0, int(used_total or 0))
    row.used_down = used_total
    row.used_up = 0
    if int(row.data_limit or 0) > 0 and used_total >= int(row.data_limit):
        row.enable = False
    await db.flush()
    return row


async def reset_client_usage(
    db: AsyncSession,
    panel: Panel,
    email: str,
    *,
    re_enable: bool = True,
    rebuild: bool = True,
    sync_nginx: bool = True,
) -> AstraNodeClient | None:
    """Zero cumulative usage for one Astra node client and re-prime Xray baseline.

    Clears ``used_*`` and sets ``stats_counter=None`` so the next stats sync
    adopts the live Xray counter without re-adding old traffic. Also mirrors
    ``PanelAccount.used_traffic = 0`` for this email on the panel.
    """
    row = await get_client(db, panel.id, email)
    if not row:
        return None
    was_enabled = bool(row.enable)
    row.used_down = 0
    row.used_up = 0
    row.stats_counter = None
    if re_enable:
        row.enable = True
    await db.flush()
    try:
        await _push_client_usage_to_accounts(db, panel.id, {email: 0})
    except Exception:  # noqa: BLE001
        logger.debug("reset push account usage failed", exc_info=True)
    # Only reload xray when enable flipped (usage counters are DB-only)
    if rebuild and bool(row.enable) != was_enabled:
        await rebuild_xray_config(db, panel, sync_nginx=sync_nginx)
    return row


async def set_client_enable(
    db: AsyncSession, panel: Panel, email: str, enable: bool
) -> AstraNodeClient | None:
    row = await get_client(db, panel.id, email)
    if not row:
        return None
    row.enable = bool(enable)
    await db.flush()
    await rebuild_xray_config(db, panel)
    return row


async def list_clients(db: AsyncSession, panel_id: int) -> list[AstraNodeClient]:
    result = await db.execute(
        select(AstraNodeClient)
        .where(AstraNodeClient.panel_id == panel_id)
        .order_by(AstraNodeClient.id.desc())
    )
    return list(result.scalars().all())


async def list_clients_enriched(db: AsyncSession, panel: Panel) -> list[dict[str, Any]]:
    """Clients on this Astra node + PanelAccount / User / group coefficient."""
    from app.models.group import Group, GroupPanel
    from app.models.user import PanelAccount, User
    from app.services.quota import norm_coeff
    from app.services.users import subscription_url as user_subscription_url

    # Fresh stats before listing
    try:
        await sync_traffic_from_xray(db, panel)
    except Exception:  # noqa: BLE001
        pass

    clients = await list_clients(db, panel.id)
    if not clients:
        return []

    accounts = (
        await db.execute(
            select(PanelAccount, User, Group)
            .join(User, User.id == PanelAccount.user_id)
            .outerjoin(Group, Group.id == PanelAccount.group_id)
            .where(PanelAccount.panel_id == panel.id)
        )
    ).all()

    by_email: dict[str, Any] = {}
    for acc, user, group in accounts:
        key = (acc.remote_username or user.username or "").strip()
        if key:
            by_email[key] = (acc, user, group)

    # GroupPanel configs for coefficient + limit_ip fallback
    gp_rows = (
        await db.execute(select(GroupPanel).where(GroupPanel.panel_id == panel.id))
    ).scalars().all()
    gp_by_group: dict[int, dict] = {
        int(g.group_id): (g.config if isinstance(g.config, dict) else {})
        for g in gp_rows
    }

    outbounds = await list_outbounds(db, panel.id)
    out: list[dict[str, Any]] = []
    for c in clients:
        used = int(c.used_up or 0) + int(c.used_down or 0)
        acc_t = by_email.get(c.email)
        acc = user = group = None
        if acc_t:
            acc, user, group = acc_t
        coeff = 1.0
        gp_cfg: dict = {}
        if acc and acc.group_id is not None:
            gp_cfg = gp_by_group.get(int(acc.group_id), {})
            coeff = norm_coeff(gp_cfg.get("coefficient", 1))
        elif acc and getattr(acc, "applied_coefficient", None):
            coeff = norm_coeff(acc.applied_coefficient)
        weighted = int(round(used * coeff))
        links = _client_links(panel, c, outbounds) if c.enable else []
        limit_ip = int(c.limit_ip or 0)
        if limit_ip <= 0 and gp_cfg:
            try:
                limit_ip = max(
                    0, int(gp_cfg.get("limit_ip", gp_cfg.get("ip_limit", 0)) or 0)
                )
            except (TypeError, ValueError):
                limit_ip = 0
        out.append(
            {
                "id": c.id,
                "panel_id": c.panel_id,
                "email": c.email,
                "uuid": c.uuid,
                "enable": bool(c.enable),
                "data_limit": int(c.data_limit or 0),
                "used_traffic": used,
                "weighted_used": weighted,
                "coefficient": coeff,
                "limit_ip": limit_ip,
                "expire": int(c.expire or 0),
                "on_hold_expire_duration": (
                    int(getattr(user, "on_hold_expire_duration", 0) or 0)
                    if user
                    else 0
                ),
                "links_count": len(links),
                "share_links": links,
                "subscription_url": user_subscription_url(user) if user else None,
                "user_id": user.id if user else None,
                "astra_username": user.username if user else None,
                "user_status": (
                    user.status.value
                    if user and hasattr(user.status, "value")
                    else (str(user.status) if user else None)
                ),
                "group_id": group.id if group else (acc.group_id if acc else None),
                "group_name": group.name if group else None,
                "account_status": (
                    acc.status.value
                    if acc and hasattr(acc.status, "value")
                    else (str(acc.status) if acc else None)
                ),
                "account_used_traffic": int(acc.used_traffic or 0) if acc else None,
                "created_at": c.created_at,
            }
        )
    return out


def node_health(panel: Panel) -> dict[str, Any]:
    xdir = panel_xray_dir(panel)
    cfg = xdir / "config.json"
    listen = panel_listen_port(panel)
    listen_host = panel_listen_host(panel)
    configured = panel_public_host(panel)
    link_host = panel_client_host(panel)
    net = panel_inbound_network(panel)
    port_open = False
    try:
        with socket.create_connection(
            (listen_host if listen_host not in ("0.0.0.0", "::") else "127.0.0.1", listen),
            timeout=0.8,
        ):
            port_open = True
    except OSError:
        port_open = False
    dns_ok, dns_note = public_host_dns_ok(
        configured, allow_cloudflare=(net in ("ws", "xhttp"))
    )
    ws_path = panel_ws_path(panel) if net in ("ws", "xhttp") else ""
    if net == "ws":
        inbound_label = "vless+ws+tls"
    elif net == "xhttp":
        inbound_label = "vless+xhttp+tls"
    else:
        inbound_label = "vless+reality"
    return {
        "xray_dir": str(xdir),
        "config_exists": cfg.is_file(),
        "listen_port": listen,
        "listen_host": listen_host,
        "public_host": link_host,
        "public_port": panel_public_port(panel),
        "public_ip": str((panel.meta or {}).get("public_ip") or ""),
        "ws_path": ws_path,
        "inbound": inbound_label,
        "reality_sni": str((panel.meta or {}).get("reality_sni") or DEFAULT_REALITY_SNI),
        "listen_open": port_open,
        "dns_ok": dns_ok,
        "dns_note": dns_note,
        "ok": cfg.is_file() and port_open,
    }


async def cleanup_panel_node(panel_id: int, meta: dict | None = None) -> None:
    """Stop Xray unit, remove config dir, refresh nginx WS include after panel delete."""
    pid = int(panel_id)
    helper = "/usr/local/bin/astra-helper"
    for cmd in (
        ["sudo", helper, "xray-stop", str(pid)],
        [helper, "xray-stop", str(pid)],
        ["sudo", "systemctl", "disable", "--now", f"astra-xray@{pid}"],
        ["systemctl", "stop", f"astra-xray@{pid}"],
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode == 0:
                break
        except Exception:  # noqa: BLE001
            continue

    # Prefer path from meta; fall back to default layout
    xdir: Path | None = None
    custom = str((meta or {}).get("xray_dir") or "").strip()
    if custom:
        xdir = Path(custom)
    else:
        xdir = DEFAULT_XRAY_DIR / f"panel_{pid}"
    try:
        if xdir and xdir.is_dir() and "astra" in str(xdir).replace("\\", "/"):
            shutil.rmtree(xdir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.warning("astra cleanup rmtree %s failed", xdir)

    # Rewrite nginx include without this panel (DB row already gone)
    try:
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await sync_nginx_ws_upstreams(db)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("astra cleanup nginx sync failed", exc_info=True)
