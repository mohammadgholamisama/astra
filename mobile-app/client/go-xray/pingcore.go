// Package pingcore is compiled with gomobile bind into an Android AAR
// and/or used alongside LibXray for share-link → Xray JSON conversion.
//
// Build example:
//
//	gomobile bind -target=android -o pingcore.aar ./go-xray
package pingcore

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
)

// HashFingerprint builds SHA-256(address:port:uuid:path:sni).
func HashFingerprint(address string, port int, uuid, path, sni string) string {
	raw := strings.ToLower(address) + ":" + strconv.Itoa(port) + ":" +
		strings.ToLower(uuid) + ":" + path + ":" + strings.ToLower(sni)
	sum := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(sum[:])
}

// BuildConfigJSON converts a share link into a minimal Xray config JSON.
// If socksPort > 0, a local SOCKS inbound is included for delay probing.
func BuildConfigJSON(shareLink string, socksPort int) (string, error) {
	outbound, err := parseShareToOutbound(strings.TrimSpace(shareLink))
	if err != nil {
		return "", err
	}

	cfg := map[string]any{
		"log": map[string]any{"loglevel": "warning"},
		"outbounds": []any{
			outbound,
			map[string]any{"protocol": "freedom", "tag": "direct"},
		},
	}
	if socksPort > 0 {
		cfg["inbounds"] = []any{
			map[string]any{
				"listen":   "127.0.0.1",
				"port":     socksPort,
				"protocol": "socks",
				"settings": map[string]any{"udp": false, "auth": "noauth"},
				"tag":      "socks-in",
			},
		}
	}
	b, err := json.Marshal(cfg)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

func parseShareToOutbound(link string) (map[string]any, error) {
	lower := strings.ToLower(link)
	switch {
	case strings.HasPrefix(lower, "vmess://"):
		return parseVMess(link)
	case strings.HasPrefix(lower, "vless://"):
		return parseVLESS(link)
	case strings.HasPrefix(lower, "trojan://"):
		return parseTrojan(link)
	case strings.HasPrefix(lower, "ss://"):
		return parseShadowsocks(link)
	default:
		return nil, fmt.Errorf("unsupported protocol")
	}
}

func parseVMess(link string) (map[string]any, error) {
	payload := strings.TrimPrefix(link, "vmess://")
	payload = strings.TrimPrefix(payload, "VMESS://")
	raw, err := base64.RawStdEncoding.DecodeString(padB64(payload))
	if err != nil {
		raw, err = base64.StdEncoding.DecodeString(padB64(payload))
		if err != nil {
			return nil, err
		}
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, err
	}
	addr, _ := m["add"].(string)
	if addr == "" {
		addr, _ = m["host"].(string)
	}
	port := anyToInt(m["port"])
	id, _ := m["id"].(string)
	net, _ := m["net"].(string)
	if net == "" {
		net = "tcp"
	}
	path, _ := m["path"].(string)
	host, _ := m["host"].(string)
	tls, _ := m["tls"].(string)
	sni, _ := m["sni"].(string)
	if sni == "" {
		sni = host
	}
	stream := map[string]any{"network": net}
	if tls == "tls" || tls == "1" {
		stream["security"] = "tls"
		stream["tlsSettings"] = map[string]any{"serverName": sni, "allowInsecure": false}
	}
	switch net {
	case "ws":
		stream["wsSettings"] = map[string]any{"path": path, "headers": map[string]any{"Host": host}}
	case "grpc":
		stream["grpcSettings"] = map[string]any{"serviceName": path}
	}
	return map[string]any{
		"protocol": "vmess",
		"settings": map[string]any{
			"vnext": []any{
				map[string]any{
					"address": addr,
					"port":    port,
					"users": []any{
						map[string]any{"id": id, "alterId": anyToInt(m["aid"]), "security": "auto"},
					},
				},
			},
		},
		"streamSettings": stream,
		"tag":            "proxy",
	}, nil
}

func parseVLESS(link string) (map[string]any, error) {
	u, err := url.Parse(link)
	if err != nil {
		return nil, err
	}
	uuid := u.User.Username()
	host := u.Hostname()
	port, _ := strconv.Atoi(u.Port())
	q := u.Query()
	network := first(q.Get("type"), "tcp")
	security := first(q.Get("security"), "none")
	sni := first(q.Get("sni"), q.Get("host"), host)
	path := q.Get("path")
	stream := map[string]any{"network": network, "security": security}
	if security == "tls" || security == "reality" {
		key := "tlsSettings"
		if security == "reality" {
			key = "realitySettings"
		}
		settings := map[string]any{"serverName": sni}
		if security == "reality" {
			settings["publicKey"] = q.Get("pbk")
			settings["shortId"] = q.Get("sid")
			settings["fingerprint"] = first(q.Get("fp"), "chrome")
		}
		stream[key] = settings
	}
	switch network {
	case "ws":
		stream["wsSettings"] = map[string]any{"path": path, "headers": map[string]any{"Host": first(q.Get("host"), sni)}}
	case "grpc":
		stream["grpcSettings"] = map[string]any{"serviceName": first(q.Get("serviceName"), path)}
	}
	return map[string]any{
		"protocol": "vless",
		"settings": map[string]any{
			"vnext": []any{
				map[string]any{
					"address": host,
					"port":    port,
					"users": []any{
						map[string]any{"id": uuid, "encryption": "none", "flow": q.Get("flow")},
					},
				},
			},
		},
		"streamSettings": stream,
		"tag":            "proxy",
	}, nil
}

func parseTrojan(link string) (map[string]any, error) {
	u, err := url.Parse(link)
	if err != nil {
		return nil, err
	}
	password, _ := u.User.Password()
	if password == "" {
		password = u.User.Username()
	}
	host := u.Hostname()
	port, _ := strconv.Atoi(u.Port())
	q := u.Query()
	network := first(q.Get("type"), "tcp")
	sni := first(q.Get("sni"), q.Get("peer"), host)
	stream := map[string]any{
		"network":  network,
		"security": "tls",
		"tlsSettings": map[string]any{
			"serverName":    sni,
			"allowInsecure": false,
		},
	}
	if network == "ws" {
		stream["wsSettings"] = map[string]any{"path": q.Get("path"), "headers": map[string]any{"Host": first(q.Get("host"), sni)}}
	}
	return map[string]any{
		"protocol": "trojan",
		"settings": map[string]any{
			"servers": []any{
				map[string]any{"address": host, "port": port, "password": password},
			},
		},
		"streamSettings": stream,
		"tag":            "proxy",
	}, nil
}

func parseShadowsocks(link string) (map[string]any, error) {
	raw := strings.TrimPrefix(link, "ss://")
	raw = strings.TrimPrefix(raw, "SS://")
	remark := ""
	if i := strings.Index(raw, "#"); i >= 0 {
		remark = raw[i+1:]
		raw = raw[:i]
		_ = remark
	}
	if !strings.Contains(raw, "@") {
		decoded, err := base64.RawStdEncoding.DecodeString(padB64(raw))
		if err != nil {
			decoded, err = base64.StdEncoding.DecodeString(padB64(raw))
			if err != nil {
				return nil, err
			}
		}
		raw = string(decoded)
	}
	u, err := url.Parse("ss://" + raw)
	if err != nil {
		return nil, err
	}
	user := u.User.Username()
	pass, _ := u.User.Password()
	method := user
	if pass == "" && strings.Contains(user, ":") {
		parts := strings.SplitN(user, ":", 2)
		method, pass = parts[0], parts[1]
	}
	host := u.Hostname()
	port, _ := strconv.Atoi(u.Port())
	return map[string]any{
		"protocol": "shadowsocks",
		"settings": map[string]any{
			"servers": []any{
				map[string]any{
					"address":  host,
					"port":     port,
					"method":   method,
					"password": pass,
				},
			},
		},
		"tag": "proxy",
	}, nil
}

func padB64(s string) string {
	s = strings.TrimSpace(s)
	s = strings.ReplaceAll(s, "-", "+")
	s = strings.ReplaceAll(s, "_", "/")
	switch len(s) % 4 {
	case 2:
		s += "=="
	case 3:
		s += "="
	}
	return s
}

func anyToInt(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case string:
		n, _ := strconv.Atoi(t)
		return n
	default:
		return 0
	}
}

func first(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
