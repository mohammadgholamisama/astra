package com.v2rayping.xray

import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLDecoder

/**
 * Builds Xray JSON for Libv2ray.measureOutboundDelay — aligned with v2rayNG
 * getV2rayConfig4Speedtest outbound shape (minimal: outbounds + freedom).
 */
object XrayConfigBuilder {

  fun fromShareLink(link: String): String? {
    val outbound = outboundFromShareLink(link) ?: return null
    return JSONObject()
      .put("log", JSONObject().put("loglevel", "error"))
      .put(
        "outbounds",
        JSONArray()
          .put(outbound)
          .put(JSONObject().put("protocol", "freedom").put("tag", "direct")),
      )
      .toString()
  }

  fun fromShareLinkWithSocks(link: String, socksPort: Int): String? {
    val outbound = outboundFromShareLink(link) ?: return null
    val port = socksPort.coerceIn(1025, 65535)
    val inbound =
      JSONObject()
        .put("tag", "socks-in")
        .put("listen", "127.0.0.1")
        .put("port", port)
        .put("protocol", "socks")
        .put(
          "settings",
          JSONObject().put("udp", false).put("auth", "noauth"),
        )
    return JSONObject()
      .put("log", JSONObject().put("loglevel", "error"))
      .put("inbounds", JSONArray().put(inbound))
      .put(
        "outbounds",
        JSONArray()
          .put(outbound)
          .put(JSONObject().put("protocol", "freedom").put("tag", "direct")),
      )
      .toString()
  }

  private fun outboundFromShareLink(link: String): JSONObject? {
    val raw = link.trim()
    if (raw.isEmpty()) return null
    val lower = raw.lowercase()
    return when {
      lower.startsWith("vless://") -> parseVless(raw)
      lower.startsWith("vmess://") -> parseVmess(raw)
      lower.startsWith("trojan://") -> parseTrojan(raw)
      lower.startsWith("ss://") -> parseShadowsocks(raw)
      else -> null
    }
  }

  private fun normalizeNetwork(raw: String): String {
    return when (raw.lowercase()) {
      "raw", "tcp" -> "tcp"
      "h2", "http" -> "h2"
      "httpupgrade" -> "httpupgrade"
      "xhttp", "splithttp" -> "xhttp"
      "ws", "websocket" -> "ws"
      "grpc" -> "grpc"
      "kcp", "mkcp" -> "kcp"
      "quic" -> "quic"
      else -> raw.lowercase().ifBlank { "tcp" }
    }
  }

  private fun parseVless(link: String): JSONObject? {
    val noHash = link.substringBefore('#')
    val body = noHash.removePrefix("vless://").removePrefix("VLESS://")
    val at = body.lastIndexOf('@')
    if (at < 0) return null
    val uuid = decode(body.substring(0, at)).trim()
    if (uuid.isEmpty()) return null
    val hostPart = body.substring(at + 1)
    val qIdx = hostPart.indexOf('?')
    val main = if (qIdx >= 0) hostPart.substring(0, qIdx) else hostPart
    val query = if (qIdx >= 0) parseQuery(hostPart.substring(qIdx + 1)) else emptyMap()
    val colon = main.lastIndexOf(':')
    if (colon < 0) return null
    val address = main.substring(0, colon).trim('[', ']')
    val port = main.substring(colon + 1).toIntOrNull() ?: return null

    val network = normalizeNetwork(query["type"] ?: query["net"] ?: "tcp")
    var security = (query["security"] ?: query["tls"] ?: "none").lowercase()
    if (security == "xtls") security = "reality"
    if (!query["pbk"].isNullOrBlank() || !query["sid"].isNullOrBlank()) {
      security = "reality"
    }
    val sni = query["sni"] ?: query["peer"] ?: address
    val headerHost = query["host"] ?: sni
    val flow = query["flow"] ?: ""
    val alpn = query["alpn"]
    val fp = query["fp"] ?: if (security == "reality" || security == "tls") "chrome" else ""

    val stream = JSONObject().put("network", network).put("security", security)
    when (security) {
      "reality" -> {
        val pbk = query["pbk"] ?: return null
        if (pbk.isBlank()) return null
        val reality =
          JSONObject()
            .put("serverName", sni)
            .put("fingerprint", fp.ifBlank { "chrome" })
            .put("publicKey", pbk)
            .put("shortId", query["sid"] ?: "")
            .put("spiderX", query["spx"] ?: "")
            .put("show", false)
        stream.put("realitySettings", reality)
      }
      "tls" -> {
        val tls =
          JSONObject()
            .put("serverName", sni)
            .put(
              "allowInsecure",
              query["allowInsecure"] == "1" ||
                query["insecure"] == "1" ||
                query["allowInsecure"] == "true",
            )
        if (fp.isNotBlank()) tls.put("fingerprint", fp)
        putAlpn(tls, alpn)
        stream.put("tlsSettings", tls)
      }
    }
    applyTransport(stream, network, query, headerHost, sni)

    val user =
      JSONObject()
        .put("id", uuid)
        .put("encryption", query["encryption"] ?: "none")
    if (flow.isNotBlank()) user.put("flow", flow)

    val settings =
      JSONObject().put(
        "vnext",
        JSONArray().put(
          JSONObject()
            .put("address", address)
            .put("port", port)
            .put("users", JSONArray().put(user)),
        ),
      )
    val pe = query["packetEncoding"]
    if (!pe.isNullOrBlank() && pe != "none") {
      settings.put("packetEncoding", pe)
    }

    return JSONObject()
      .put("protocol", "vless")
      .put("settings", settings)
      .put("streamSettings", stream)
      .put("tag", "proxy")
  }

  private fun parseVmess(link: String): JSONObject? {
    val payload = link.removePrefix("vmess://").removePrefix("VMESS://").substringBefore('#')
    val json = try {
      val decoded = String(Base64.decode(padB64(payload), Base64.DEFAULT))
      JSONObject(decoded)
    } catch (_: Throwable) {
      return null
    }
    val address = json.optString("add").ifEmpty { json.optString("host") }
    val port = json.optInt("port", 0)
    val id = json.optString("id")
    if (address.isEmpty() || port <= 0 || id.isEmpty()) return null
    val network = normalizeNetwork(json.optString("net", "tcp").ifEmpty { "tcp" })
    val tls = json.optString("tls")
    val security = when {
      tls.equals("tls", true) || tls == "1" -> "tls"
      tls.equals("reality", true) -> "reality"
      else -> "none"
    }
    val sni = json.optString("sni").ifEmpty { json.optString("host") }.ifEmpty { address }
    val path = json.optString("path")
    val host = json.optString("host").ifEmpty { sni }
    val fp = json.optString("fp").ifEmpty { if (security == "tls") "chrome" else "" }

    val stream = JSONObject().put("network", network).put("security", security)
    if (security == "tls") {
      val tlsObj =
        JSONObject()
          .put("serverName", sni)
          .put("allowInsecure", false)
      if (fp.isNotBlank()) tlsObj.put("fingerprint", fp)
      stream.put("tlsSettings", tlsObj)
    }
    val q =
      mapOf(
        "path" to path,
        "host" to host,
        "type" to network,
        "headerType" to json.optString("type"),
        "serviceName" to json.optString("path"),
      )
    applyTransport(stream, network, q, host, sni)

    return JSONObject()
      .put("protocol", "vmess")
      .put(
        "settings",
        JSONObject().put(
          "vnext",
          JSONArray().put(
            JSONObject()
              .put("address", address)
              .put("port", port)
              .put(
                "users",
                JSONArray().put(
                  JSONObject()
                    .put("id", id)
                    .put("alterId", json.optInt("aid", 0))
                    .put("security", json.optString("scy").ifEmpty { "auto" }),
                ),
              ),
          ),
        ),
      )
      .put("streamSettings", stream)
      .put("tag", "proxy")
  }

  private fun parseTrojan(link: String): JSONObject? {
    val noHash = link.substringBefore('#')
    val body = noHash.removePrefix("trojan://").removePrefix("TROJAN://")
    val at = body.lastIndexOf('@')
    if (at < 0) return null
    val password = decode(body.substring(0, at))
    val hostPart = body.substring(at + 1)
    val qIdx = hostPart.indexOf('?')
    val main = if (qIdx >= 0) hostPart.substring(0, qIdx) else hostPart
    val query = if (qIdx >= 0) parseQuery(hostPart.substring(qIdx + 1)) else emptyMap()
    val colon = main.lastIndexOf(':')
    if (colon < 0) return null
    val address = main.substring(0, colon).trim('[', ']')
    val port = main.substring(colon + 1).toIntOrNull() ?: return null
    val network = normalizeNetwork(query["type"] ?: "tcp")
    val sni = query["sni"] ?: query["peer"] ?: address
    val headerHost = query["host"] ?: sni
    val fp = query["fp"] ?: "chrome"

    val stream =
      JSONObject()
        .put("network", network)
        .put("security", "tls")
    val tls =
      JSONObject()
        .put("serverName", sni)
        .put(
          "allowInsecure",
          query["allowInsecure"] == "1" || query["insecure"] == "1",
        )
        .put("fingerprint", fp)
    putAlpn(tls, query["alpn"])
    stream.put("tlsSettings", tls)
    applyTransport(stream, network, query, headerHost, sni)

    return JSONObject()
      .put("protocol", "trojan")
      .put(
        "settings",
        JSONObject().put(
          "servers",
          JSONArray().put(
            JSONObject()
              .put("address", address)
              .put("port", port)
              .put("password", password),
          ),
        ),
      )
      .put("streamSettings", stream)
      .put("tag", "proxy")
  }

  private fun parseShadowsocks(link: String): JSONObject? {
    var raw = link.removePrefix("ss://").removePrefix("SS://").substringBefore('#')
    val query: Map<String, String>
    if (raw.contains('?')) {
      val qi = raw.indexOf('?')
      query = parseQuery(raw.substring(qi + 1))
      raw = raw.substring(0, qi)
    } else {
      query = emptyMap()
    }
    if (!raw.contains('@')) {
      raw = try {
        String(Base64.decode(padB64(raw), Base64.DEFAULT))
      } catch (_: Throwable) {
        return null
      }
    }
    val at = raw.lastIndexOf('@')
    if (at < 0) return null
    val userInfo = raw.substring(0, at)
    val hostPort = raw.substring(at + 1).substringBefore('/')
    val colon = hostPort.lastIndexOf(':')
    if (colon < 0) return null
    val address = hostPort.substring(0, colon).trim('[', ']')
    val port = hostPort.substring(colon + 1).toIntOrNull() ?: return null
    val method: String
    val password: String
    if (userInfo.contains(':')) {
      val p = userInfo.split(':', limit = 2)
      method = p[0]
      password = p.getOrElse(1) { "" }
    } else {
      val decoded = try {
        String(Base64.decode(padB64(userInfo), Base64.DEFAULT))
      } catch (_: Throwable) {
        return null
      }
      val p = decoded.split(':', limit = 2)
      method = p[0]
      password = p.getOrElse(1) { "" }
    }

    val outbound =
      JSONObject()
        .put("protocol", "shadowsocks")
        .put(
          "settings",
          JSONObject().put(
            "servers",
            JSONArray().put(
              JSONObject()
                .put("address", address)
                .put("port", port)
                .put("method", method)
                .put("password", password),
            ),
          ),
        )
        .put("tag", "proxy")

    val network = normalizeNetwork(query["type"] ?: query["net"] ?: "")
    if (network.isNotBlank() && network != "tcp") {
      val security = (query["security"] ?: "none").lowercase()
      val sni = query["sni"] ?: query["host"] ?: address
      val stream = JSONObject().put("network", network).put("security", security)
      if (security == "tls") {
        stream.put(
          "tlsSettings",
          JSONObject()
            .put("serverName", sni)
            .put("allowInsecure", query["allowInsecure"] == "1"),
        )
      }
      applyTransport(stream, network, query, query["host"] ?: sni, sni)
      outbound.put("streamSettings", stream)
    }
    return outbound
  }

  private fun applyTransport(
    stream: JSONObject,
    network: String,
    query: Map<String, String>,
    headerHost: String,
    sni: String,
  ) {
    when (network) {
      "ws" ->
        stream.put(
          "wsSettings",
          JSONObject()
            .put("path", query["path"] ?: "/")
            .put("headers", JSONObject().put("Host", headerHost)),
        )
      "grpc" ->
        stream.put(
          "grpcSettings",
          JSONObject()
            .put("serviceName", query["serviceName"] ?: query["path"] ?: "")
            .put("multiMode", query["mode"] == "multi"),
        )
      "tcp" -> {
        if ((query["headerType"] ?: query["type"] ?: "") == "http") {
          stream.put(
            "tcpSettings",
            JSONObject().put(
              "header",
              JSONObject()
                .put("type", "http")
                .put(
                  "request",
                  JSONObject().put(
                    "headers",
                    JSONObject().put("Host", JSONArray().put(headerHost)),
                  ),
                ),
            ),
          )
        }
      }
      "httpupgrade" ->
        stream.put(
          "httpupgradeSettings",
          JSONObject()
            .put("path", query["path"] ?: "/")
            .put("host", headerHost),
        )
      "xhttp" ->
        stream.put(
          "xhttpSettings",
          JSONObject()
            .put("path", query["path"] ?: "/")
            .put("host", headerHost)
            .put("mode", query["mode"] ?: "auto"),
        )
      "h2" ->
        stream.put(
          "httpSettings",
          JSONObject()
            .put("path", query["path"] ?: "/")
            .put("host", JSONArray().put(headerHost)),
        )
      "kcp" ->
        stream.put(
          "kcpSettings",
          JSONObject()
            .put("mtu", 1350)
            .put("tti", 50)
            .put("uplinkCapacity", 12)
            .put("downlinkCapacity", 100)
            .put("congestion", false)
            .put("readBufferSize", 2)
            .put("writeBufferSize", 2)
            .put(
              "header",
              JSONObject().put("type", query["headerType"] ?: "none"),
            )
            .put("seed", query["seed"] ?: query["path"] ?: ""),
        )
      "quic" ->
        stream.put(
          "quicSettings",
          JSONObject()
            .put("security", query["quicSecurity"] ?: "none")
            .put("key", query["key"] ?: "")
            .put(
              "header",
              JSONObject().put("type", query["headerType"] ?: "none"),
            ),
        )
    }
  }

  private fun putAlpn(target: JSONObject, alpn: String?) {
    if (alpn.isNullOrBlank()) return
    val arr = JSONArray()
    for (p in alpn.split(',')) {
      val t = p.trim()
      if (t.isNotEmpty()) arr.put(t)
    }
    if (arr.length() > 0) target.put("alpn", arr)
  }

  private fun parseQuery(qs: String): Map<String, String> {
    if (qs.isEmpty()) return emptyMap()
    return qs.split('&').mapNotNull { part ->
      if (part.isEmpty()) return@mapNotNull null
      val i = part.indexOf('=')
      if (i < 0) return@mapNotNull part to ""
      decode(part.substring(0, i)) to decode(part.substring(i + 1))
    }.toMap()
  }

  private fun decode(s: String): String = try {
    URLDecoder.decode(s, "UTF-8")
  } catch (_: Throwable) {
    s
  }

  private fun padB64(s: String): String {
    var t = s.replace('-', '+').replace('_', '/')
    val pad = (4 - t.length % 4) % 4
    if (pad > 0) t += "=".repeat(pad)
    return t
  }
}
