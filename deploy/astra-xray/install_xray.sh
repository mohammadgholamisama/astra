#!/usr/bin/env bash
# Install Xray-core + enable template unit for Astra outbound nodes.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

XRAY_BIN=/usr/local/bin/xray
if [ ! -x "$XRAY_BIN" ]; then
  bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi

install -d -m 755 /opt/astra/xray
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/astra-xray@.service"
if [ -f "$UNIT_SRC" ]; then
  install -m 644 "$UNIT_SRC" /etc/systemd/system/astra-xray@.service
else
  cat >/etc/systemd/system/astra-xray@.service <<'EOF'
[Unit]
Description=Astra Xray node for panel %i
After=network.target
[Service]
Type=simple
ExecStartPre=/bin/mkdir -p /opt/astra/xray/panel_%i
ExecStart=/usr/local/bin/xray run -config /opt/astra/xray/panel_%i/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
echo "OK: xray at $XRAY_BIN — enable with: systemctl enable --now astra-xray@PANEL_ID"
