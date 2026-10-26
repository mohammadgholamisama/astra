package com.v2rayping.xray

import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLDecoder

/**
 * Builds a minimal Xray JSON config from a share link for Libv2ray.measureOutboundDelay.
 * Supports vless (incl. Reality), vmess, trojan, ss.
 */
object XrayConfigBuilder {

  fun fromShareLink(link: String): String? {
    val raw = link.trim()
    if (raw.isEmpty()) return null
    val lower = raw.lowercase()
    val outbound = when {
      lower.startsWith("vless://") -> parseVless(raw)
      lower.startsWith("vmess://") -> parseVmess(raw)
      lower.startsWith("trojan://") -> parseTrojan(raw)
      lower.startsWith("ss://") -> parseShadowsocks(raw)
      else -> null
    } ?: return null

    return JSONObject()
      .put("log", JSONObject().put("loglevel", "warning"))
      .put(
        "outbounds",
        JSONArray()
          .put(outbound)
          .put(JSONObject().put("protocol", "freedom").put("tag", "direct")),
      )
      .toString()
  }

  private fun parseVless(link: String): JSONObject? {
    val noHash = link.substringBefore('#')
    val body = noHash.removePrefix("vless://").removePrefix("VLESS://")
    val at = body.lastIndexOf('@')
    if (at < 0) return null
    val uuid = body.substring(0, at)
    val hostPart = body.substring(at + 1)
    val qIdx = hostPart.indexOf('?')
    val main = if (qIdx >= 0) hostPart.substring(0, qIdx) else hostPart
    val query = if (qIdx >= 0) parseQuery(hostPart.substring(qIdx + 1)) else emptyMap()
    val colon = main.lastIndexOf(':')
    if (colon < 0) return null
    val address = main.substring(0, colon).trim('[', ']')
    val port = main.substring(colon + 1).toIntOrNull() ?: return null

    val network = (query["type"] ?: query["net"] ?: "tcp").lowercase()
    var security = (query["security"] ?: query["tls"] ?: "none").lowercase()
    if (query.containsKey("pbk") || query.containsKey("sid")) security = "reality"
    val sni = query["sni"] ?: query["host"] ?: address
    val flow = query["flow"] ?: ""

    val stream = JSONObject().put("network", network).put("security", security)
    when (security) {
      "reality" -> {
        stream.put(
          "realitySettings",
          JSONObject()
            .put("serverName", sni)
            .put("fingerprint", query["fp"] ?: "chrome")
            .put("publicKey", query["pbk"] ?: "")
            .put("shortId", query["sid"] ?: "")
            .put("spiderX", query["spx"] ?: "/")
            .put("show", false),
        )
      }
      "tls" -> {
        stream.put(
          "tlsSettings",
          JSONObject()
            .put("serverName", sni)
            .put("allowInsecure", query["allowInsecure"] == "1")
            .put("fingerprint", query["fp"] ?: ""),
        )
      }
    }
    when (network) {
      "ws" -> stream.put(
        "wsSettings",
        JSONObject()
          .put("path", query["path"] ?: "/")
          .put("headers", JSONObject().put("Host", query["host"] ?: sni)),
      )
      "grpc" -> stream.put(
        "grpcSettings",
        JSONObject().put("serviceName", query["serviceName"] ?: query["path"] ?: ""),
      )
      "tcp" -> {
        if ((query["headerType"] ?: "") == "http") {
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
                    JSONObject().put("Host", JSONArray().put(query["host"] ?: sni)),
                  ),
                ),
            ),
          )
        }
      }
      "httpupgrade", "xhttp", "splithttp" -> {
        stream.put(
          "httpupgradeSettings",
          JSONObject()
            .put("path", query["path"] ?: "/")
            .put("host", query["host"] ?: sni),
        )
      }
    }

    val user = JSONObject()
      .put("id", uuid)
      .put("encryption", "none")
      .put("flow", flow)

    return JSONObject()
      .put("protocol", "vless")
      .put(
        "settings",
        JSONObject().put(
          "vnext",
          JSONArray().put(
            JSONObject()
              .put("address", address)
              .put("port", port)
              .put("users", JSONArray().put(user)),
          ),
        ),
      )
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
    val network = json.optString("net", "tcp").ifEmpty { "tcp" }
    val tls = json.optString("tls")
    val security = when {
      tls == "tls" || tls == "1" -> "tls"
      else -> "none"
    }
    val sni = json.optString("sni").ifEmpty { json.optString("host") }
    val path = json.optString("path")
    val host = json.optString("host")

    val stream = JSONObject().put("network", network).put("security", security)
    if (security == "tls") {
      stream.put(
        "tlsSettings",
        JSONObject().put("serverName", sni).put("allowInsecure", false),
      )
    }
    if (network == "ws") {
      stream.put(
        "wsSettings",
        JSONObject().put("path", path).put("headers", JSONObject().put("Host", host)),
      )
    }

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
                    .put("security", "auto"),
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
    val password = body.substring(0, at)
    val hostPart = body.substring(at + 1)
    val qIdx = hostPart.indexOf('?')
    val main = if (qIdx >= 0) hostPart.substring(0, qIdx) else hostPart
    val query = if (qIdx >= 0) parseQuery(hostPart.substring(qIdx + 1)) else emptyMap()
    val colon = main.lastIndexOf(':')
    if (colon < 0) return null
    val address = main.substring(0, colon).trim('[', ']')
    val port = main.substring(colon + 1).toIntOrNull() ?: return null
    val network = (query["type"] ?: "tcp").lowercase()
    val sni = query["sni"] ?: query["peer"] ?: address

    val stream = JSONObject()
      .put("network", network)
      .put("security", "tls")
      .put(
        "tlsSettings",
        JSONObject().put("serverName", sni).put("allowInsecure", false),
      )
    if (network == "ws") {
      stream.put(
        "wsSettings",
        JSONObject()
          .put("path", query["path"] ?: "/")
          .put("headers", JSONObject().put("Host", query["host"] ?: sni)),
      )
    }

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
    val hostPort = raw.substring(at + 1).substringBefore('?')
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

    return JSONObject()
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
