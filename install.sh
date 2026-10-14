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

check_port_80() {
  if ! is_port_in_use 80; then
    ok "Port 80 is free (nginx)"
    return 0
  fi
  local who=""
  if command -v ss >/dev/null 2>&1; then
    who="$(ss -lntp 2>/dev/null | grep -E ':80\s' || true)"
  fi
  if echo "$who" | grep -qi nginx; then
    warn "Port 80 already used by nginx — will reuse / adjust site"
  else
    warn "Port 80 is busy by another process:"
    echo "$who"
    warn "Certbot / HTTP access may fail until port 80 is free for nginx."
  fi
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

  if [[ -f "$envf" && "${REFRESH_ONLY:-0}" == "1" ]]; then
    # ensure helper path + port key exist
    if ! grep -qE '^ASTRA_HELPER=' "$envf"; then
      echo "ASTRA_HELPER=${HELPER_BIN}" >> "$envf"
    fi
    if ! grep -qE '^ASTRA_APP_PORT=' "$envf"; then
      echo "ASTRA_APP_PORT=${port}" >> "$envf"
    fi
    ok "Keeping existing .env"
    # export for later steps
    ADMIN_USERNAME="$(grep -E '^ADMIN_USERNAME=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' "$envf" | cut -d= -f2- | tr -d '\r')"
    ASTRA_APP_PORT="$(grep -E '^ASTRA_APP_PORT=' "$envf" | cut -d= -f2- | tr -d '\r')"
    return 0
  fi

  if [[ -f "$envf" ]]; then
    warn ".env exists — not overwriting secrets"
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
  ok ".env created"
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
  [[ -f "$unit_src" ]] || die "Missing $unit_src"
  install -m 644 "$unit_src" /etc/systemd/system/astra.service
  systemctl daemon-reload
  systemctl enable astra
  ok "systemd unit installed"
}

install_nginx_site() {
  local port="$1"
  local tpl="${ASTRA_ROOT}/deploy/nginx-http.conf.tpl"
  [[ -f "$tpl" ]] || die "Missing $tpl"
  sed "s/__ASTRA_PORT__/${port}/g" "$tpl" > "$NGINX_SITE"
  ln -sfn "$NGINX_SITE" "$NGINX_LINK"
  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
  fi
  nginx -t
  systemctl enable nginx
  systemctl restart nginx
  ok "nginx site configured (proxy → 127.0.0.1:${port})"
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
  echo -e "${GREEN}${BOLD}"
  cat <<EOF
╔══════════════════════════════════════════════════════════╗
║              Astra installed successfully!               ║
║                 نصب با موفقیت انجام شد                    ║
╠══════════════════════════════════════════════════════════╣
║  Panel URL : http://${ip}
║  (nginx :80 → app :${port})
║                                                          ║
║  Username  : ${ADMIN_USERNAME}
║  Password  : ${ADMIN_PASSWORD}
║                                                          ║
║  Manage    : x-astra                                     ║
║  (domain / SSL / port / update)                          ║
║                                                          ║
║  Data      : ${ASTRA_ROOT}/data/astra.db                 ║
║  Env       : ${ASTRA_ROOT}/.env                          ║
╚══════════════════════════════════════════════════════════╝
EOF
  echo -e "${NC}"
  info "Save the password now. / همین الان رمز را ذخیره کنید."
  info "Run: ${BOLD}x-astra${NC}  to set domain & SSL."
}

# ─── Main ────────────────────────────────────────────────────
main() {
  REFRESH_ONLY=0
  banner
  require_root
  detect_os
  handle_existing

  step "Install system packages / نصب پکیج‌ها"
  install_base_packages
  install_nodejs

  step "Deploy project to ${ASTRA_ROOT}"
  ensure_user
  deploy_project

  step "Pick free port & check :80"
  local port ip
  if [[ "${REFRESH_ONLY}" == "1" && -f "${ASTRA_ROOT}/.env" ]]; then
    port="$(grep -E '^ASTRA_APP_PORT=' "${ASTRA_ROOT}/.env" | cut -d= -f2- | tr -d '\r')"
    port="${port:-$(pick_free_port)}"
  else
    port="$(pick_free_port)"
  fi
  ok "Selected app port: $port"
  check_port_80
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

  step "Install systemd, nginx, CLI, sudoers"
  install_systemd
  install_nginx_site "$port"
  install_cli_bins
  install_sudoers
  fix_perms

  step "Enable & start services"
  start_services

  success_box "$ip" "$port"
}

main "$@"
