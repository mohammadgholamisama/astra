#!/usr/bin/env bash
# Astra Panel — one-command Linux installer (Ubuntu/Debian)
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/mohammadgholamisama/astra.git/main/install.sh)
#   git clone … && bash install.sh
set -euo pipefail

ASTRA_ROOT="/opt/astra"
ASTRA_USER="astra"
REPO_URL="https://github.com/mohammadgholamisama/astra.git"
HELPER_BIN="/usr/local/bin/astra-helper"
MENU_BIN="/usr/local/bin/x-astra"
NGINX_SITE="/etc/nginx/sites-available/astra"
NGINX_LINK="/etc/nginx/sites-enabled/astra"
SUDOERS_DST="/etc/sudoers.d/astra"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

STEP=0
TOTAL_STEPS=8

info()  { echo -e "${CYAN}ℹ${NC} $*"; }
ok()    { echo -e "${GREEN}✔${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC} $*"; }
err()   { echo -e "${RED}✖${NC} $*" >&2; }
die()   { err "$*"; exit 1; }

step() {
  STEP=$((STEP + 1))
  echo
  echo -e "${BOLD}${CYAN}[${STEP}/${TOTAL_STEPS}]${NC} ${BOLD}$*${NC}"
}

banner() {
  echo -e "${BOLD}${CYAN}"
  cat <<'EOF'
    ___   _____ ________  ___
   /   | / ___//_  __/ _ \/   |
  / /| | \__ \  / / / , _/ /| |
 / ___ |___/ / / / / /| / ___ |
/_/  |_/____/ /_/ /_/ |_/_/  |_|
EOF
  echo -e "${NC}${BOLD}  Astra Panel Installer${NC}"
  echo -e "  ${CYAN}─────────────────────────────────${NC}"
  echo
}

# ─── OS / root ───────────────────────────────────────────────
require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "Run as root. / با کاربر root اجرا کنید: sudo bash install.sh"
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
  else
    die "Cannot detect OS. / سیستم‌عامل شناسایی نشد."
  fi
  case "$OS_ID" in
    ubuntu|debian) ok "OS: $PRETTY_NAME" ;;
    *)
      if [[ "$OS_LIKE" == *debian* || "$OS_LIKE" == *ubuntu* ]]; then
        warn "OS $PRETTY_NAME (debian-like) — continuing"
      else
        die "Unsupported OS: ${PRETTY_NAME:-$OS_ID}. Ubuntu/Debian required."
      fi
      ;;
  esac
}

# ─── Helpers ─────────────────────────────────────────────────
is_port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]:?${port}$"
  elif [[ -e /proc/net/tcp ]]; then
    # fallback: bash /dev/tcp
    (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null
  else
    (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null
  fi
}

pick_free_port() {
  local candidates=(2086 2087 8000 8080 8888 3000 9000 9090 5000 7000)
  local p
  for p in "${candidates[@]}"; do
    if ! is_port_in_use "$p"; then
      echo "$p"
      return 0
    fi
    info "Port $p busy — trying next…"
  done
  # scan upward from 2088
  for p in $(seq 2088 2200); do
    if ! is_port_in_use "$p"; then
      echo "$p"
      return 0
    fi
  done
  die "No free TCP port found for uvicorn."
}

count_other_nginx_sites() {
  local n=0 f base
  shopt -s nullglob
  for f in /etc/nginx/sites-enabled/*; do
    base="$(basename "$f")"
    [[ "$base" == "astra" ]] && continue
    n=$((n + 1))
  done
  shopt -u nullglob
  echo "$n"
}

detect_share_mode() {
  # Shared = existing site(s) or non-nginx already on :80 → never steal default_server
  ASTRA_SHARE=0
  local others
  others="$(count_other_nginx_sites)"
  if [[ "$others" -gt 0 ]]; then
    ASTRA_SHARE=1
    warn "Found $others existing nginx site(s) — shared mode (won't take :80 default)"
    return 0
  fi
  if is_port_in_use 80; then
    local who=""
    if command -v ss >/dev/null 2>&1; then
      who="$(ss -lntp 2>/dev/null | grep -E ':80\s' || true)"
    fi
    if echo "$who" | grep -qi nginx; then
      # nginx on 80 but no other enabled sites yet — exclusive OK
      ok "Port 80 used by nginx with no other sites — exclusive mode"
    else
      ASTRA_SHARE=1
      warn "Port 80 busy by non-nginx process — shared mode"
      echo "$who"
    fi
  else
    ok "Port 80 free — exclusive mode (Astra can use default :80)"
  fi
}

check_port_80() {
  detect_share_mode
}

server_ip() {
  local ip
  ip="$(curl -4 -fsS --max-time 8 ifconfig.me 2>/dev/null || true)"
  if [[ -z "$ip" ]]; then
    ip="$(curl -4 -fsS --max-time 8 icanhazip.com 2>/dev/null || true)"
  fi
  if [[ -z "$ip" ]]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  echo "${ip:-127.0.0.1}"
}

rand_hex() {
  openssl rand -hex 24
}

rand_password() {
  # readable: letters+digits, 32 chars — no fixed defaults on install
  tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 32 || openssl rand -base64 24 | tr -d '/+=' | head -c 32
}

rand_username() {
  # random admin username (never hardcode "admin")
  local u
  u="$(tr -dc 'a-z0-9' </dev/urandom 2>/dev/null | head -c 10 || openssl rand -hex 5)"
  echo "u${u}"
}

script_source_dir() {
  # When curl|bash, BASH_SOURCE may be /dev/fd/… without project files
  local src="${BASH_SOURCE[0]:-}"
  if [[ -n "$src" && -f "$src" ]]; then
    cd "$(dirname "$src")" && pwd
  else
    echo ""
  fi
}

is_local_project() {
  local d="$1"
  [[ -n "$d" && -f "$d/backend/requirements.txt" && -f "$d/frontend/package.json" ]]
}

# ─── Idempotent / update ─────────────────────────────────────
handle_existing() {
  if [[ ! -d "$ASTRA_ROOT" || ! -f "$ASTRA_ROOT/.env" ]]; then
    return 0
  fi
  echo
  warn "Astra already installed at $ASTRA_ROOT"
  echo "  1) Update (git pull / rebuild / restart)"
  echo "  2) Reinstall configs only (keep data/.env)"
  echo "  3) Exit"
  read -r -p "Choice [1/2/3]: " choice
  case "${choice:-3}" in
    1)
      info "Updating existing install…"
      ensure_user
      if [[ -d "$ASTRA_ROOT/.git" ]]; then
        git -C "$ASTRA_ROOT" pull --ff-only || die "git pull failed"
      else
        warn "No .git — syncing from installer source if available"
        local src
        src="$(script_source_dir)"
        if is_local_project "$src"; then
          rsync -a \
            --exclude '.venv' --exclude 'node_modules' --exclude 'dist' \
            --exclude 'frontend/dist' --exclude 'data' --exclude '.env' \
            "$src"/ "$ASTRA_ROOT"/
        fi
      fi
      install_base_packages
      install_nodejs
      install_python_deps
      build_frontend
      install_cli_bins
      install_sudoers
      install -m 644 "${ASTRA_ROOT}/deploy/astra.service" /etc/systemd/system/astra.service
      systemctl daemon-reload
      systemctl restart astra
      systemctl reload nginx || true
      ok "Updated. Manage with: x-astra"
      exit 0
      ;;
    2)
      info "Refreshing systemd / nginx / CLI bins; keeping .env and data"
      REFRESH_ONLY=1
      ;;
    *)
      info "Exit cleanly. / خروج."
      exit 0
      ;;
  esac
}

# ─── Packages ────────────────────────────────────────────────
apt_update_once() {
  if [[ "${APT_UPDATED:-0}" != "1" ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    APT_UPDATED=1
  fi
}

install_base_packages() {
  apt_update_once
  apt-get install -y \
    curl git nginx certbot python3 python3-venv python3-pip \
    openssl ca-certificates gnupg lsb-release \
    python3-certbot-nginx rsync sudo
  ok "Base packages installed"
}

install_nodejs() {
  if command -v node >/dev/null 2>&1; then
    local major
    major="$(node -v | sed 's/^v//' | cut -d. -f1)"
    if [[ "$major" -ge 20 ]]; then
      ok "Node.js $(node -v) already present"
      return 0
    fi
    warn "Node $(node -v) < 20 — upgrading"
  fi

  info "Installing Node.js 20 (NodeSource)…"
  if curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; then
    apt_update_once
    apt-get install -y nodejs
  else
    warn "NodeSource failed — trying nvm fallback"
    export NVM_DIR="/usr/local/nvm"
    mkdir -p "$NVM_DIR"
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | NVM_DIR="$NVM_DIR" bash
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh"
    nvm install 20
    nvm alias default 20
    ln -sfn "$(command -v node)" /usr/local/bin/node
    ln -sfn "$(command -v npm)" /usr/local/bin/npm
  fi

  command -v node >/dev/null || die "Node.js install failed"
  command -v npm >/dev/null || die "npm install failed"
  ok "Node.js $(node -v) / npm $(npm -v)"
}

# ─── User / files ────────────────────────────────────────────
ensure_user() {
  if id "$ASTRA_USER" >/dev/null 2>&1; then
    ok "User $ASTRA_USER exists"
  else
    useradd --system --home "$ASTRA_ROOT" --shell /usr/sbin/nologin "$ASTRA_USER"
    ok "Created system user $ASTRA_USER"
  fi
}

deploy_project() {
  local src
  src="$(script_source_dir)"

  mkdir -p "$ASTRA_ROOT"

  if is_local_project "$src"; then
    info "Copying local project from $src → $ASTRA_ROOT"
    rsync -a --delete \
      --exclude '.venv' \
      --exclude 'node_modules' \
      --exclude 'dist' \
      --exclude 'frontend/dist' \
      --exclude 'backend/.venv' \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      --exclude 'data' \
      --exclude '.env' \
      --exclude 'astra.db' \
      --exclude 'backend/astra.db' \
      "$src"/ "$ASTRA_ROOT"/
    # keep .git if present (for updates)
    ok "Project synced (local)"
  else
    if [[ -d "$ASTRA_ROOT/.git" ]]; then
      info "Updating existing git clone…"
      git -C "$ASTRA_ROOT" fetch --depth 1 origin || true
      git -C "$ASTRA_ROOT" pull --ff-only || warn "pull failed — using existing tree"
    elif [[ -f "$ASTRA_ROOT/backend/requirements.txt" ]]; then
      warn "Project files exist without .git — skipping clone"
    else
      info "Cloning $REPO_URL → $ASTRA_ROOT"
      # if dir non-empty but incomplete, clone into temp then move
      if [[ -z "$(ls -A "$ASTRA_ROOT" 2>/dev/null || true)" ]]; then
        git clone --depth 1 "$REPO_URL" "$ASTRA_ROOT"
      else
        local tmp
        tmp="$(mktemp -d)"
        git clone --depth 1 "$REPO_URL" "$tmp/astra"
        rsync -a "$tmp/astra"/ "$ASTRA_ROOT"/
        rm -rf "$tmp"
      fi
    fi
    ok "Project ready (git)"
  fi

  mkdir -p "$ASTRA_ROOT/data" "$ASTRA_ROOT/deploy"
  # ensure deploy templates exist even if older clone
  if [[ -n "$src" && -d "$src/deploy" ]]; then
    rsync -a "$src/deploy/" "$ASTRA_ROOT/deploy/"
  fi
}

write_env() {
  local port="$1" ip="$2"
  local envf="${ASTRA_ROOT}/.env"

  ensure_env_key() {
    local k="$1" v="$2"
    if ! grep -qE "^${k}=" "$envf"; then
      echo "${k}=${v}" >> "$envf"
    fi
  }

  if [[ -f "$envf" && "${REFRESH_ONLY:-0}" == "1" ]]; then
    ensure_env_key ASTRA_HELPER "$HELPER_BIN"
    ensure_env_key ASTRA_APP_PORT "$port"
    ensure_env_key ASTRA_NGINX_SHARE "${ASTRA_SHARE:-0}"
    ensure_env_key ASTRA_BIND_HOST "$([ "${ASTRA_SHARE:-0}" = 1 ] && echo 0.0.0.0 || echo 127.0.0.1)"
    ensure_env_key DATABASE_URL "sqlite+aiosqlite:////opt/astra/data/astra.db"
    ok "Keeping existing .env"
    ADMIN_USERNAME="$(grep -E '^ADMIN_USERNAME=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ASTRA_APP_PORT="$(grep -E '^ASTRA_APP_PORT=' "$envf" | cut -d= -f2- | tr -d '\r')"
    return 0
  fi

  if [[ -f "$envf" ]]; then
    warn ".env exists — not overwriting secrets"
    ensure_env_key ASTRA_HELPER "$HELPER_BIN"
    ensure_env_key ASTRA_NGINX_SHARE "${ASTRA_SHARE:-0}"
    ensure_env_key ASTRA_BIND_HOST "$([ "${ASTRA_SHARE:-0}" = 1 ] && echo 0.0.0.0 || echo 127.0.0.1)"
    ensure_env_key DATABASE_URL "sqlite+aiosqlite:////opt/astra/data/astra.db"
    ADMIN_USERNAME="$(grep -E '^ADMIN_USERNAME=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ASTRA_APP_PORT="$(grep -E '^ASTRA_APP_PORT=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ASTRA_APP_PORT="${ASTRA_APP_PORT:-$port}"
    return 0
  fi

  local secret pass
  secret="$(rand_hex)"
  pass="$(rand_password)"
  ADMIN_USERNAME="$(rand_username)"
  ADMIN_PASSWORD="$pass"
  ASTRA_APP_PORT="$port"

  local sub_url panel_url
  if [[ "$port" == "80" ]]; then
    sub_url="http://${ip}"
  else
    # public via nginx :80
    sub_url="http://${ip}"
  fi
  panel_url="http://${ip}"

  local share_flag=0 bind_host="127.0.0.1"
  if [[ "${ASTRA_SHARE:-0}" == "1" ]]; then
    share_flag=1
    bind_host="0.0.0.0"
    panel_url="http://${ip}:${port}"
    sub_url="http://${ip}:${port}"
  fi

  # Always isolated SQLite under /opt/astra/data — never touches system PostgreSQL
  local db_path="/opt/astra/data/astra.db"
  if [[ -f "$db_path" ]]; then
    ok "Keeping existing Astra SQLite DB: $db_path"
  else
    info "New isolated database will be created: $db_path"
  fi

  cat > "$envf" <<EOF
DATABASE_URL=sqlite+aiosqlite:////opt/astra/data/astra.db
SECRET_KEY=${secret}
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
PANEL_NAME=Astra
SUB_BASE_URL=${sub_url}
PANEL_BASE_URL=${panel_url}
CORS_ORIGINS=*
ASTRA_APP_PORT=${ASTRA_APP_PORT}
ASTRA_BIND_HOST=${bind_host}
ASTRA_NGINX_SHARE=${share_flag}
ASTRA_HELPER=${HELPER_BIN}
ASTRA_PANEL_DOMAIN=
ASTRA_SUB_DOMAIN=
ASTRA_SSL_PANEL=0
ASTRA_SSL_SUB=0
FRONTEND_DIST=/opt/astra/frontend/dist
ACCESS_TOKEN_EXPIRE_MINUTES=1440
EOF
  chmod 640 "$envf"
  chown root:"$ASTRA_USER" "$envf"
  ok ".env created (share=${share_flag}, bind=${bind_host})"
}

install_python_deps() {
  local venv="${ASTRA_ROOT}/backend/.venv"
  info "Python venv → $venv"
  python3 -m venv "$venv"
  # shellcheck disable=SC1091
  source "$venv/bin/activate"
  pip install --upgrade pip wheel
  pip install -r "${ASTRA_ROOT}/backend/requirements.txt"
  deactivate
  chown -R "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/backend"
  ok "Python dependencies ready"
}

build_frontend() {
  info "Building frontend (VITE_API_BASE empty = same-origin)…"
  cd "${ASTRA_ROOT}/frontend"
  export VITE_API_BASE=""
  if [[ -f package-lock.json ]]; then
    npm ci || npm install
  else
    npm install
  fi
  npm run build
  mkdir -p "${ASTRA_ROOT}/frontend/dist"
  chown -R "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/frontend/dist"
  ok "Frontend built → frontend/dist"
}

install_systemd() {
  local unit_src="${ASTRA_ROOT}/deploy/astra.service"
  local upd_src="${ASTRA_ROOT}/deploy/astra-update.service"
  [[ -f "$unit_src" ]] || die "Missing $unit_src"
  install -m 644 "$unit_src" /etc/systemd/system/astra.service
  if [[ -f "$upd_src" ]]; then
    install -m 644 "$upd_src" /etc/systemd/system/astra-update.service
  fi
  systemctl daemon-reload
  systemctl enable astra
  ok "systemd unit installed"
}

install_nginx_site() {
  local port="$1"
  # Prefer helper (respects shared/exclusive). Falls back to tpl only if helper missing.
  systemctl enable nginx 2>/dev/null || true
  if [[ -x "$HELPER_BIN" ]]; then
    "$HELPER_BIN" write-nginx
    ok "nginx site via astra-helper (share=${ASTRA_SHARE:-0}, app :${port})"
    return 0
  fi
  local tpl="${ASTRA_ROOT}/deploy/nginx-http.conf.tpl"
  [[ -f "$tpl" ]] || die "Missing $tpl"
  if [[ "${ASTRA_SHARE:-0}" == "1" ]]; then
    # Shared: empty placeholder site — IP access is uvicorn on bind 0.0.0.0:port
    cat > "$NGINX_SITE" <<EOF
# Astra shared mode — other sites untouched. Set a domain with x-astra.
EOF
  else
    sed "s/__ASTRA_PORT__/${port}/g" "$tpl" > "$NGINX_SITE"
    if [[ -L /etc/nginx/sites-enabled/default ]]; then
      rm -f /etc/nginx/sites-enabled/default
    fi
  fi
  ln -sfn "$NGINX_SITE" "$NGINX_LINK"
  nginx -t
  systemctl restart nginx
  ok "nginx site configured"
}

init_database() {
  # Create tables in isolated SQLite only (idempotent; never touches Postgres)
  info "Initializing Astra database schema…"
  sudo -u "$ASTRA_USER" bash -c "
    set -e
    cd '${ASTRA_ROOT}/backend'
    . .venv/bin/activate
    python - <<'PY'
import asyncio
from app.core.schema_sync import ensure_schema
from app.seed import seed_admin

async def main():
    await ensure_schema()
    await seed_admin()
    print('schema+seed ok')

asyncio.run(main())
PY
  " || die "Database init failed"
  ok "Database ready at /opt/astra/data/astra.db"
}

install_cli_bins() {
  install -m 755 "${ASTRA_ROOT}/deploy/astra-helper" "$HELPER_BIN"
  install -m 755 "${ASTRA_ROOT}/deploy/x-astra" "$MENU_BIN"
  # keep copies in place
  chmod 755 "${ASTRA_ROOT}/deploy/astra-helper" "${ASTRA_ROOT}/deploy/x-astra"
  ok "Installed $MENU_BIN and $HELPER_BIN"
}

install_sudoers() {
  local src="${ASTRA_ROOT}/deploy/sudoers.astra"
  [[ -f "$src" ]] || die "Missing $src"
  install -m 440 "$src" "$SUDOERS_DST"
  if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$SUDOERS_DST" || die "Invalid sudoers file"
  fi
  ok "sudoers: $ASTRA_USER → astra-helper"
}

fix_perms() {
  mkdir -p "${ASTRA_ROOT}/data"
  chown -R "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/data"
  chown -R "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/backend"
  # root owns .env with group astra
  chown root:"$ASTRA_USER" "${ASTRA_ROOT}/.env"
  chmod 640 "${ASTRA_ROOT}/.env"
  # allow astra to read app code
  chown -R root:"$ASTRA_USER" "$ASTRA_ROOT"
  chmod -R g+rX "$ASTRA_ROOT"
  chown -R "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/data" "${ASTRA_ROOT}/backend/.venv"
  # frontend dist readable by nginx (www-data)
  chmod -R a+rX "${ASTRA_ROOT}/frontend/dist" 2>/dev/null || true
}

start_services() {
  systemctl restart astra
  systemctl reload nginx || systemctl restart nginx
  sleep 2
  if systemctl is-active --quiet astra; then
    ok "astra service is running"
  else
    err "astra failed to start — check: journalctl -u astra -n 50"
    systemctl status astra --no-pager || true
  fi
}

success_box() {
  local ip="$1" port="$2"
  echo
  local url share="${ASTRA_SHARE:-0}"
  if [[ "$share" == "1" ]]; then
    url="http://${ip}:${port}"
  else
    url="http://${ip}"
  fi
  echo -e "${GREEN}${BOLD}"
  cat <<EOF
╔══════════════════════════════════════════════════════════╗
║              Astra installed successfully!               ║
╠══════════════════════════════════════════════════════════╣
║  Panel URL : ${url}
║  App port  : ${port}   share_mode=${share}
║                                                          ║
║  Username  : ${ADMIN_USERNAME}
║  Password  : ${ADMIN_PASSWORD}
║                                                          ║
║  Manage    : x-astra                                     ║
║  (domain / SSL / port / update / uninstall)              ║
║                                                          ║
║  Database  : ${ASTRA_ROOT}/data/astra.db (isolated)      ║
║  Env       : ${ASTRA_ROOT}/.env                          ║
╚══════════════════════════════════════════════════════════╝
EOF
  echo -e "${NC}"
  info "Save the admin password now."
  if [[ "$share" == "1" ]]; then
    warn "Shared server: existing sites untouched. Open firewall for port ${port} if needed."
    info "Or set a panel domain in x-astra (menu 3) for :80/:443 on that hostname only."
  fi
  info "Run: ${BOLD}x-astra${NC}"
}

# ─── Main ────────────────────────────────────────────────────
main() {
  REFRESH_ONLY=0
  ASTRA_SHARE=0
  banner
  require_root
  detect_os
  handle_existing

  step "Install system packages"
  install_base_packages
  install_nodejs

  step "Deploy project to ${ASTRA_ROOT}"
  ensure_user
  deploy_project

  step "Pick free port & detect existing sites"
  local port ip
  check_port_80
  if [[ "${REFRESH_ONLY}" == "1" && -f "${ASTRA_ROOT}/.env" ]]; then
    port="$(grep -E '^ASTRA_APP_PORT=' "${ASTRA_ROOT}/.env" | cut -d= -f2- | tr -d '\r')"
    port="${port:-$(pick_free_port)}"
  else
    port="$(pick_free_port)"
  fi
  ok "Selected app port: $port (share_mode=${ASTRA_SHARE})"
  ip="$(server_ip)"
  ok "Server IP: $ip"

  step "Create .env and data directory"
  write_env "$port" "$ip"
  port="${ASTRA_APP_PORT:-$port}"
  mkdir -p "${ASTRA_ROOT}/data"
  chown "$ASTRA_USER":"$ASTRA_USER" "${ASTRA_ROOT}/data"

  step "Python venv + pip install"
  install_python_deps

  step "Build frontend"
  build_frontend

  step "Install systemd, CLI, sudoers, nginx"
  install_systemd
  install_cli_bins
  install_sudoers
  install_nginx_site "$port"
  fix_perms

  step "Init isolated database + start services"
  init_database
  start_services

  success_box "$ip" "$port"
}

main "$@"
