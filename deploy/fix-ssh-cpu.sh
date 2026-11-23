#!/bin/bash
# Run on the NEW panel host as root after a reboot (or when SSH works again).
# Hardens sshd + caps Astra CPU so SSH/login never get starved.
set -euo pipefail

ROOT=/opt/astra
[[ -d "$ROOT" ]] || { echo "missing $ROOT"; exit 1; }

echo "== sshd harden =="
install -m 644 "$ROOT/deploy/sshd-astra-harden.conf" /etc/ssh/sshd_config.d/60-astra-harden.conf
sshd -t
# Avoid systemctl when D-Bus is wedged («Transport endpoint is not connected»)
if systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null; then
  :
else
  kill -HUP "$(cat /run/sshd.pid 2>/dev/null || pgrep -n sshd)" 2>/dev/null || true
fi

echo "== astra CPU guard =="
install -m 644 "$ROOT/deploy/astra.service" /etc/systemd/system/astra.service
mkdir -p /etc/systemd/system/astra.service.d
cat > /etc/systemd/system/astra.service.d/cpu-guard.conf <<'EOF'
[Service]
CPUQuota=250%
TasksMax=4096
OOMPolicy=continue
EOF
systemctl daemon-reload 2>/dev/null || true
if ! systemctl restart astra 2>/dev/null; then
  # D-Bus/systemd socket broken in some web consoles — restart by PID.
  pkill -f 'uvicorn app.main:app' 2>/dev/null || true
  sleep 1
  # Unit may still respawn if systemd is healthy; else start manually:
  if ! systemctl is-active astra >/dev/null 2>&1; then
    sudo -u astra bash -lc 'cd /opt/astra/backend && nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 2086 --limit-concurrency 120 --timeout-keep-alive 5 >/tmp/astra-uvicorn.log 2>&1 &' || true
  fi
fi

echo "== nginx stub (avoid reload failures) =="
mkdir -p /opt/astra/nginx
[[ -f /opt/astra/nginx/astra-xray-ws.conf ]] || echo '# stub' > /opt/astra/nginx/astra-xray-ws.conf

echo "== postgres bind localhost only (if open to world) =="
if [[ -f /etc/postgresql/*/main/postgresql.conf ]]; then
  conf=$(echo /etc/postgresql/*/main/postgresql.conf)
  if grep -q "^listen_addresses" "$conf"; then
    sed -i "s/^listen_addresses.*/listen_addresses = '127.0.0.1'/" "$conf"
  else
    echo "listen_addresses = '127.0.0.1'" >>"$conf"
  fi
  systemctl reload postgresql 2>/dev/null || true
fi

echo "== apply panel update (CPU+usage fixes) =="
/usr/local/bin/astra-helper apply-update || true

echo "== status =="
uptime
systemctl is-active astra ssh nginx 2>/dev/null || systemctl is-active astra sshd nginx
curl -m 5 -fsS http://127.0.0.1:2086/health || true
echo
echo "DONE — SSH should stay usable under load now."
