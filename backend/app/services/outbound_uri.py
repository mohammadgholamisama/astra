"""Parse share-link URIs (vless/vmess/trojan/ss) for Astra outbound pool."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


@dataclass
class ParsedOutbound:
    uri: str
    protocol: str
    host: str
    port: int
    remark: str
    uuid: str | None
    password: str | None
    params: dict[str, str]
    raw: dict[str, Any]

    @property
    def uri_hash(self) -> str:
        norm = re.sub(r"#.*$", "", self.uri.strip())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _qs(params: dict[str, list[str]]) -> dict[str, str]:
    return {k: (v[0] if v else "") for k, v in params.items()}


def parse_outbound_uri(raw: str) -> ParsedOutbound:
    uri = (raw or "").strip()
    if not uri:
        raise ValueError("لینک خالی است")
    if "://" not in uri:
        raise ValueError("فرمت لینک نامعتبر است")

    scheme = uri.split("://", 1)[0].lower()
    if scheme not in ("vless", "vmess", "trojan", "ss", "shadowsocks"):
        raise ValueError(f"پروتکل پشتیبانی نمی‌شود: {scheme}")

    if scheme == "vmess":
        return _parse_vmess(uri)
    if scheme in ("ss", "shadowsocks"):
        return _parse_ss(uri)

    # vless / trojan
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    port = int(parsed.port or 0)
    user = unquote(parsed.username or "")
    params = _qs(parse_qs(parsed.query))
    remark = unquote(parsed.fragment or "") or f"{scheme}-{host}-{port}"
    if not host or port <= 0:
        raise ValueError("host/port نامعتبر در لینک")
    return ParsedOutbound(
        uri=uri,
        protocol=scheme,
        host=host,
        port=port,
        remark=remark[:255],
        uuid=user if scheme == "vless" else None,
        password=user if scheme == "trojan" else None,
        params=params,
        raw={
            "scheme": scheme,
            "host": host,
            "port": port,
            "user": user,
            "params": params,
            "remark": remark,
        },
    )


def _parse_vmess(uri: str) -> ParsedOutbound:
    b64 = uri.split("://", 1)[1].strip()
    if "#" in b64:
        b64, frag = b64.split("#", 1)
        remark_fallback = unquote(frag)
    else:
        remark_fallback = ""
    pad = "=" * ((4 - len(b64) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"vmess نامعتبر: {exc}") from exc
    host = str(data.get("add") or data.get("host") or "")
    port = int(data.get("port") or 0)
    uuid = str(data.get("id") or "")
    remark = str(data.get("ps") or remark_fallback or f"vmess-{host}-{port}")
    if not host or port <= 0:
        raise ValueError("vmess بدون host/port")
    return ParsedOutbound(
        uri=uri,
        protocol="vmess",
        host=host,
        port=port,
        remark=remark[:255],
        uuid=uuid or None,
        password=None,
        params={k: str(v) for k, v in data.items() if k not in ("add", "port", "id", "ps")},
        raw=data if isinstance(data, dict) else {},
    )


def _parse_ss(uri: str) -> ParsedOutbound:
    # ss://METHOD:PASSWORD@HOST:PORT#remark  or ss://base64
    body = uri.split("://", 1)[1]
    remark = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        remark = unquote(frag)
    if "@" not in body:
        pad = "=" * ((4 - len(body) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode(body + pad).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"ss نامعتبر: {exc}") from exc
        body = decoded
    # METHOD:PASSWORD@HOST:PORT
    if "@" not in body:
        raise ValueError("ss بدون @")
    userinfo, hostport = body.rsplit("@", 1)
    method, _, password = userinfo.partition(":")
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, 0
    if not host or port <= 0:
        raise ValueError("ss بدون host/port")
    remark = remark or f"ss-{host}-{port}"
    return ParsedOutbound(
        uri=uri,
        protocol="shadowsocks",
        host=host,
        port=port,
        remark=remark[:255],
        uuid=None,
        password=password,
        params={"method": method},
        raw={"method": method, "password": password, "host": host, "port": port},
    )


def parse_outbound_lines(text: str) -> list[ParsedOutbound]:
    out: list[ParsedOutbound] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = parse_outbound_uri(line)
        if p.uri_hash in seen:
            continue
        seen.add(p.uri_hash)
        out.append(p)
    return out


def xray_outbound_tag(outbound_id: int) -> str:
    return f"ob-{int(outbound_id)}"


def to_xray_outbound(outbound_id: int, parsed: dict[str, Any], protocol: str) -> dict[str, Any]:
    """Build one Xray outbound object from stored parsed fields."""
    tag = xray_outbound_tag(outbound_id)
    proto = (protocol or "").lower()
    host = str(parsed.get("host") or "")
    port = int(parsed.get("port") or 0)
    params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
    if not params and isinstance(parsed, dict):
        # vmess raw stores fields at top level
        params = {k: str(v) for k, v in parsed.items() if isinstance(v, (str, int, float, bool))}

    if proto == "vless":
        user = str(parsed.get("user") or parsed.get("uuid") or "")
        network = str(params.get("type") or params.get("network") or "tcp")
        security = str(params.get("security") or "none")
        stream: dict[str, Any] = {"network": network, "security": security}
        if network == "ws":
            stream["wsSettings"] = {
                "path": params.get("path") or "/",
                "headers": {"Host": params.get("host") or params.get("sni") or host},
            }
        if network == "grpc":
            stream["grpcSettings"] = {
                "serviceName": params.get("serviceName") or params.get("path") or ""
            }
        if security == "tls":
            stream["tlsSettings"] = {
                "serverName": params.get("sni") or params.get("host") or host,
                "allowInsecure": True,
            }
        if security == "reality":
            from urllib.parse import unquote

            spx = params.get("spx") or ""
            try:
                spx = unquote(str(spx))
            except Exception:
                pass
            stream["realitySettings"] = {
                "serverName": params.get("sni") or host,
                "fingerprint": params.get("fp") or "chrome",
                "publicKey": params.get("pbk") or "",
                "shortId": params.get("sid") or "",
                "spiderX": spx or "",
            }
        user_obj: dict[str, Any] = {
            "id": user,
            "encryption": params.get("encryption") or "none",
        }
        flow = str(params.get("flow") or "").strip()
        if flow:
            user_obj["flow"] = flow
        return {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [user_obj],
                    }
                ]
            },
            "streamSettings": stream,
        }

    if proto == "trojan":
        password = str(parsed.get("user") or parsed.get("password") or "")
        network = str(params.get("type") or "tcp")
        stream = {
            "network": network,
            "security": params.get("security") or "tls",
            "tlsSettings": {
                "serverName": params.get("sni") or host,
                "allowInsecure": True,
            },
        }
        return {
            "tag": tag,
            "protocol": "trojan",
            "settings": {"servers": [{"address": host, "port": port, "password": password}]},
            "streamSettings": stream,
        }

    if proto in ("vmess",):
        uuid = str(parsed.get("id") or parsed.get("uuid") or "")
        network = str(parsed.get("net") or params.get("type") or "tcp")
        security = str(parsed.get("tls") or params.get("security") or "none")
        stream: dict[str, Any] = {"network": network, "security": security if security else "none"}
        if network == "ws":
            stream["wsSettings"] = {
                "path": str(parsed.get("path") or "/"),
                "headers": {"Host": str(parsed.get("host") or host)},
            }
        return {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": host,
                        "port": port,
                        "users": [
                            {
                                "id": uuid,
                                "alterId": int(parsed.get("aid") or 0),
                                "security": str(parsed.get("scy") or "auto"),
                            }
                        ],
                    }
                ]
            },
            "streamSettings": stream,
        }

    if proto in ("shadowsocks", "ss"):
        return {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [
                    {
                        "address": host,
                        "port": port,
                        "method": str(params.get("method") or "aes-256-gcm"),
                        "password": str(parsed.get("password") or ""),
                    }
                ]
            },
        }

    # Fallback direct (should not happen)
    return {"tag": tag, "protocol": "freedom", "settings": {}}


def _host_port_for_uri(host: str, port: int) -> str:
    h = (host or "").strip()
    if ":" in h and not h.startswith("["):
        h = f"[{h}]"
    return f"{h}:{int(port)}"


def xray_outbound_to_share_link(
    outbound: dict[str, Any], *, remark: str = ""
) -> str | None:
    """Best-effort reverse of to_xray_outbound for common VLESS/Trojan/VMess/SS."""
    from urllib.parse import quote, urlencode

    if not isinstance(outbound, dict):
        return None
    proto = str(outbound.get("protocol") or "").lower().strip()
    settings = outbound.get("settings") if isinstance(outbound.get("settings"), dict) else {}
    stream = (
        outbound.get("streamSettings")
        if isinstance(outbound.get("streamSettings"), dict)
        else {}
    )
    network = str(stream.get("network") or "tcp")
    security = str(stream.get("security") or "none")
    name = (remark or str(outbound.get("tag") or "")).strip() or f"{proto}-node"

    if proto == "vless":
        vnext = (settings.get("vnext") or [{}])[0] if isinstance(settings.get("vnext"), list) else {}
        users = (vnext.get("users") or [{}])[0] if isinstance(vnext.get("users"), list) else {}
        host = str(vnext.get("address") or "")
        port = int(vnext.get("port") or 0)
        uid = str(users.get("id") or "")
        if not host or port <= 0 or not uid:
            return None
        q: dict[str, str] = {
            "encryption": str(users.get("encryption") or "none"),
            "security": security,
            "type": network,
        }
        flow = str(users.get("flow") or "").strip()
        if flow:
            q["flow"] = flow
        if network == "ws":
            ws = stream.get("wsSettings") if isinstance(stream.get("wsSettings"), dict) else {}
            q["path"] = str(ws.get("path") or "/")
            headers = ws.get("headers") if isinstance(ws.get("headers"), dict) else {}
            host_h = headers.get("Host") or headers.get("host")
            if isinstance(host_h, list):
                host_h = host_h[0] if host_h else ""
            if host_h:
                q["host"] = str(host_h)
        elif network == "grpc":
            grpc = stream.get("grpcSettings") if isinstance(stream.get("grpcSettings"), dict) else {}
            q["serviceName"] = str(grpc.get("serviceName") or "")
            q["mode"] = str(grpc.get("multiMode") and "multi" or "gun")
        elif network == "tcp":
            tcp = stream.get("tcpSettings") if isinstance(stream.get("tcpSettings"), dict) else {}
            header = tcp.get("header") if isinstance(tcp.get("header"), dict) else {}
            if str(header.get("type") or "") == "http":
                q["headerType"] = "http"
                req = header.get("request") if isinstance(header.get("request"), dict) else {}
                path = req.get("path")
                if isinstance(path, list) and path:
                    q["path"] = str(path[0])
                elif path:
                    q["path"] = str(path)
                hdrs = req.get("headers") if isinstance(req.get("headers"), dict) else {}
                host_h = hdrs.get("Host") or hdrs.get("host")
                if isinstance(host_h, list):
                    host_h = host_h[0] if host_h else ""
                if host_h:
                    q["host"] = str(host_h)
        if security == "tls":
            tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
            sni = str(tls.get("serverName") or "")
            if sni:
                q["sni"] = sni
            fp = str(tls.get("fingerprint") or "")
            if fp:
                q["fp"] = fp
            alpn = tls.get("alpn")
            if isinstance(alpn, list) and alpn:
                q["alpn"] = ",".join(str(x) for x in alpn)
            elif alpn:
                q["alpn"] = str(alpn)
        elif security == "reality":
            rs = (
                stream.get("realitySettings")
                if isinstance(stream.get("realitySettings"), dict)
                else {}
            )
            if rs.get("serverName"):
                q["sni"] = str(rs["serverName"])
            if rs.get("fingerprint"):
                q["fp"] = str(rs["fingerprint"])
            if rs.get("publicKey"):
                q["pbk"] = str(rs["publicKey"])
            if rs.get("shortId"):
                q["sid"] = str(rs["shortId"])
            spx = str(rs.get("spiderX") or "")
            if spx:
                q["spx"] = spx
        return (
            f"vless://{uid}@{_host_port_for_uri(host, port)}?{urlencode(q)}#{quote(name)}"
        )

    if proto == "trojan":
        servers = settings.get("servers") if isinstance(settings.get("servers"), list) else []
        srv = servers[0] if servers else {}
        host = str(srv.get("address") or "")
        port = int(srv.get("port") or 0)
        password = str(srv.get("password") or "")
        if not host or port <= 0 or not password:
            return None
        q = {"security": security or "tls", "type": network}
        tls = stream.get("tlsSettings") if isinstance(stream.get("tlsSettings"), dict) else {}
        if tls.get("serverName"):
            q["sni"] = str(tls["serverName"])
        return (
            f"trojan://{quote(password, safe='')}@{_host_port_for_uri(host, port)}"
            f"?{urlencode(q)}#{quote(name)}"
        )

    if proto == "vmess":
        import base64

        vnext = (settings.get("vnext") or [{}])[0] if isinstance(settings.get("vnext"), list) else {}
        users = (vnext.get("users") or [{}])[0] if isinstance(vnext.get("users"), list) else {}
        host = str(vnext.get("address") or "")
        port = int(vnext.get("port") or 0)
        uid = str(users.get("id") or "")
        if not host or port <= 0 or not uid:
            return None
        tls_flag = "tls" if security in ("tls", "reality") else ""
        obj = {
            "v": "2",
            "ps": name,
            "add": host,
            "port": port,
            "id": uid,
            "aid": int(users.get("alterId") or 0),
            "scy": str(users.get("security") or "auto"),
            "net": network,
            "type": "none",
            "host": "",
            "path": "",
            "tls": tls_flag,
        }
        if network == "ws":
            ws = stream.get("wsSettings") if isinstance(stream.get("wsSettings"), dict) else {}
            obj["path"] = str(ws.get("path") or "/")
            headers = ws.get("headers") if isinstance(ws.get("headers"), dict) else {}
            host_h = headers.get("Host") or headers.get("host") or ""
            if isinstance(host_h, list):
                host_h = host_h[0] if host_h else ""
            obj["host"] = str(host_h)
        raw = base64.urlsafe_b64encode(
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"vmess://{raw}"

    if proto in ("shadowsocks", "ss"):
        servers = settings.get("servers") if isinstance(settings.get("servers"), list) else []
        srv = servers[0] if servers else {}
        host = str(srv.get("address") or "")
        port = int(srv.get("port") or 0)
        method = str(srv.get("method") or "")
        password = str(srv.get("password") or "")
        if not host or port <= 0 or not method or not password:
            return None
        userinfo = quote(f"{method}:{password}", safe="")
        return f"ss://{userinfo}@{_host_port_for_uri(host, port)}#{quote(name)}"

    return None


def share_links_from_xray_json(text: str) -> list[str]:
    """Parse Xray / v2rayN JSON subscription bodies into share-links."""
    raw = (text or "").strip()
    if not raw or raw[0] not in "[{":
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []

    configs: list[dict[str, Any]] = []
    if isinstance(data, list):
        configs = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("outbounds"), list):
            configs = [data]
        elif isinstance(data.get("configs"), list):
            configs = [x for x in data["configs"] if isinstance(x, dict)]

    out: list[str] = []
    seen: set[str] = set()
    for cfg in configs:
        remark = str(cfg.get("remarks") or cfg.get("ps") or cfg.get("remark") or "")
        outs = cfg.get("outbounds") if isinstance(cfg.get("outbounds"), list) else None
        if outs:
            for ob in outs:
                if not isinstance(ob, dict):
                    continue
                tag = str(ob.get("tag") or "").lower()
                proto = str(ob.get("protocol") or "").lower()
                if proto in ("freedom", "blackhole", "dns", "block"):
                    continue
                if tag in ("direct", "block", "dns-out", "api"):
                    continue
                link = xray_outbound_to_share_link(ob, remark=remark)
                if link and link not in seen:
                    seen.add(link)
                    out.append(link)
                # Only first proxy outbound per config object
                if link:
                    break
        else:
            # Already a single outbound-shaped object
            link = xray_outbound_to_share_link(cfg, remark=remark)
            if link and link not in seen:
                seen.add(link)
                out.append(link)
    return out
