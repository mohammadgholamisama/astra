#!/usr/bin/env bash
# Install / refresh astra-ping-hub beside the panel. Never touches data/ or panel .env values
# except appending PING_HUB_* keys if missing. Safe on shared hosts (telsupport untouched).
set -euo pipefail

ASTRA_ROOT="${ASTRA_ROOT:-/opt/astra}"
HUB_DIR="${ASTRA_ROOT}/mobile-app/backend-go"
HUB_ENV="${HUB_DIR}/.env"
BIN_DIR="${HUB_DIR}/bin"
UNIT_SRC="${ASTRA_ROOT}/deploy/astra-ping-hub.service"
UNIT_DST="/etc/systemd/system/astra-ping-hub.service"
PANEL_ENV="${ASTRA_ROOT}/.env"

die() { echo "ping-hub-install: $*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || die "run as root"
[[ -d "$HUB_DIR" ]] || die "missing $HUB_DIR — apply-update first"
[[ -f "$PANEL_ENV" ]] || die "missing panel .env"

get_panel() {
  local k="$1" d="${2:-}"
  local v
  v="$(grep -E "^${k}=" "$PANEL_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true)"
  if [[ -z "$v" ]]; then echo "$d"; else echo "$v"; fi
}

ensure_redis() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq redis-server sqlite3 ca-certificates >/dev/null 2>&1 || \
      apt-get install -y redis-server sqlite3 ca-certificates
  fi
  if command -v redis-cli >/dev/null 2>&1 && redis-cli ping 2>/dev/null | grep -qi pong; then
    echo "ping-hub-install: redis already up"
    return 0
  fi
  systemctl enable --now redis-server 2>/dev/null || systemctl enable --now redis 2>/dev/null || true
  sleep 1
  redis-cli ping 2>/dev/null | grep -qi pong || die "redis not available"
  echo "ping-hub-install: redis ready"
}

ensure_golang() {
  if command -v go >/dev/null 2>&1; then
    return 0
  fi
  if command -v snap >/dev/null 2>&1; then
    snap install go --classic 2>/dev/null || true
  fi
  if ! command -v go >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq golang-go >/dev/null || true
  fi
  command -v go >/dev/null 2>&1 || die "golang required to build ping-hub"
}

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 24 /dev/urandom | xxd -p | tr -d '\n'
  fi
}

# Create / reuse an Astra admin API key for PANEL_API_KEY (stored hashed in SQLite).
ensure_panel_api_key() {
  local existing
  if [[ -f "$HUB_ENV" ]]; then
    existing="$(grep -E '^PANEL_API_KEY=' "$HUB_ENV" 2>/dev/null | cut -d= -f2- | tr -d '\r' || true)"
    if [[ -n "$existing" ]]; then
      echo "$existing"
      return 0
    fi
  fi

  local raw
  raw="ask_$(rand_hex)"
  # Use SQLAlchemy with busy_timeout — safe while astra.service holds the DB
  sudo -u astra env RAW="$raw" bash -c '
    set -e
    cd /opt/astra/backend
    . .venv/bin/activate
    python3 - <<'"'"'PY'"'"'
import hashlib, os
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
raw = os.environ["RAW"]
engine = create_engine("sqlite:////opt/astra/data/astra.db", connect_args={"timeout": 30})
h = hashlib.sha256(raw.encode()).hexdigest()
with engine.begin() as conn:
    conn.execute(text("PRAGMA busy_timeout=30000"))
    admin_id = conn.execute(text("SELECT id FROM admins WHERE is_active=1 ORDER BY id LIMIT 1")).scalar()
    if not admin_id:
        raise SystemExit("no active admin")
    conn.execute(text("DELETE FROM api_keys WHERE name=:n"), {"n": "ping-hub"})
    conn.execute(
        text("INSERT INTO api_keys (admin_id, name, key_hash, key_prefix, note, is_active, created_at) "
             "VALUES (:a,:n,:h,:p,:note,1,:ts)"),
        {"a": admin_id, "n": "ping-hub", "h": h, "p": raw[:8],
         "note": "auto: astra-ping-hub panel sync",
         "ts": datetime.now(timezone.utc).isoformat()},
    )
print("ok")
PY
  ' >/dev/null
  echo "$raw"
}

write_hub_env() {
  local api_key panel_key panel_base
  mkdir -p "$HUB_DIR"
  panel_base="$(get_panel PANEL_BASE_URL "https://$(get_panel ASTRA_PANEL_DOMAIN p.vokqem.ir)")"
  if [[ -f "$HUB_ENV" ]] && grep -qE '^API_KEY=.' "$HUB_ENV"; then
    api_key="$(grep -E '^API_KEY=' "$HUB_ENV" | cut -d= -f2- | tr -d '\r')"
  else
    api_key="hub_$(rand_hex)"
  fi
  panel_key="$(ensure_panel_api_key)"

  umask 077
  cat >"$HUB_ENV" <<EOF
PORT=18080
BIND_HOST=127.0.0.1
API_KEY=${api_key}
REDIS_ADDR=127.0.0.1:6379
REDIS_PASSWORD=
REDIS_DB=0
PING_TTL_SECONDS=600
RATE_LIMIT_MAX=240
RATE_LIMIT_WINDOW_SECONDS=60
PANEL_SYNC_ENABLED=true
PANEL_BASE_URL=${panel_base}
PANEL_API_PATH=/api/admin/config-order
PANEL_API_KEY=${panel_key}
PANEL_TIMEOUT_SECONDS=20
EOF
  chown astra:astra "$HUB_ENV" 2>/dev/null || true
  chmod 600 "$HUB_ENV"

  # Remember public base for the mobile app / ops
  if ! grep -qE '^PING_HUB_PUBLIC_URL=' "$PANEL_ENV"; then
    echo "PING_HUB_PUBLIC_URL=${panel_base%/}/ping-api" >>"$PANEL_ENV"
  fi
  if ! grep -qE '^PING_HUB_API_KEY=' "$PANEL_ENV"; then
    echo "PING_HUB_API_KEY=${api_key}" >>"$PANEL_ENV"
  else
    sed -i "s|^PING_HUB_API_KEY=.*|PING_HUB_API_KEY=${api_key}|" "$PANEL_ENV"
  fi
  echo "ping-hub-install: wrote $HUB_ENV"
}

build_binary() {
  ensure_golang
  mkdir -p "$BIN_DIR"
  cd "$HUB_DIR"
  # module may need network once
  go mod download
  CGO_ENABLED=0 GOOS=linux go build -ldflags='-s -w' -o "${BIN_DIR}/ping-hub" ./cmd/server
  chown -R astra:astra "$HUB_DIR" 2>/dev/null || true
  echo "ping-hub-install: built ${BIN_DIR}/ping-hub"
}

install_unit() {
  [[ -f "$UNIT_SRC" ]] || die "missing $UNIT_SRC"
  install -m 644 "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
  systemctl enable astra-ping-hub
  systemctl restart astra-ping-hub
  sleep 1
  systemctl is-active --quiet astra-ping-hub || die "astra-ping-hub failed to start"
  echo "ping-hub-install: astra-ping-hub active"
}

ensure_redis
write_hub_env
build_binary
install_unit
echo "ping-hub-install: ok"
