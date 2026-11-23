#!/usr/bin/env bash
# Install Xray-core + template unit for Astra outbound nodes.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

XRAY_BIN=/usr/local/bin/xray

install_xray_binary() {
  if [ -x "$XRAY_BIN" ]; then
    echo "xray already present: $($XRAY_BIN version 2>/dev/null | head -1 || echo ok)"
    return 0
  fi

  echo "installing xray via official installer…"
  if bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install; then
    :
  else
    echo "official installer failed — trying direct zip…"
  fi

  if [ -x "$XRAY_BIN" ]; then
    return 0
  fi

  # Direct fallback (no systemd side-effects from Xray-install)
  local ver arch zip url tmp
  ver="$(curl -fsSL https://api.github.com/repos/XTLS/Xray-core/releases/latest \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  [[ -n "$ver" ]] || ver="v25.3.6"
  case "$(uname -m)" in
    x86_64|amd64) arch=64 ;;
    aarch64|arm64) arch=arm64-v8a ;;
    *) echo "unsupported arch: $(uname -m)" >&2; return 1 ;;
  esac
  zip="Xray-linux-${arch}.zip"
  url="https://github.com/XTLS/Xray-core/releases/download/${ver}/${zip}"
  tmp="$(mktemp -d)"
  echo "downloading $url"
  curl -fL --retry 3 -o "$tmp/$zip" "$url"
  unzip -o "$tmp/$zip" -d "$tmp/out"
  install -m 755 "$tmp/out/xray" "$XRAY_BIN"
  rm -rf "$tmp"
  command -v setcap >/dev/null 2>&1 && setcap 'cap_net_bind_service=+ep' "$XRAY_BIN" 2>/dev/null || true
}

install_xray_binary
[[ -x "$XRAY_BIN" ]] || { echo "FAILED: $XRAY_BIN still missing" >&2; exit 1; }
"$XRAY_BIN" version | head -3

install -d -m 755 /opt/astra/xray
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/astra-xray@.service"
if [ -f "$UNIT_SRC" ]; then
  install -m 644 "$UNIT_SRC" /etc/systemd/system/astra-xray@.service
else
  cat >/etc/systemd/system/astra-xray@.service <<'EOF'
[Unit]
Description=Astra Xray node for panel %i
After=network.target
StartLimitIntervalSec=120
StartLimitBurst=5
[Service]
Type=simple
ExecStartPre=/bin/mkdir -p /opt/astra/xray/panel_%i
ExecStart=/usr/local/bin/xray run -config /opt/astra/xray/panel_%i/config.json
Restart=on-failure
RestartSec=15
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
EOF
fi

timeout 15 systemctl daemon-reload 2>/dev/null || true
echo "OK: xray at $XRAY_BIN"
ls -la "$XRAY_BIN"
