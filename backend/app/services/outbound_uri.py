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
