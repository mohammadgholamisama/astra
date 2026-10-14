# Deploy assets

Files used by `install.sh` on Ubuntu/Debian servers:

| File | Purpose |
|------|---------|
| `astra.service` | systemd unit (`User=astra`, uvicorn on `ASTRA_APP_PORT`) |
| `nginx-http.conf.tpl` | HTTP template — placeholders `__ASTRA_PORT__` |
| `nginx-ssl.conf.tpl` | HTTPS template — `__DOMAIN__`, `__ASTRA_PORT__` |
| `x-astra` | Interactive menu (`/usr/local/bin/x-astra`) — English; includes uninstall |
| `astra-helper` | Root helper for nginx/SSL/env/uninstall (`/usr/local/bin/astra-helper`) |
| `sudoers.astra` | Passwordless sudo for `astra` → `astra-helper` only |

**Shared servers:** if other nginx sites exist, install uses `ASTRA_NGINX_SHARE=1` + bind `0.0.0.0` on a free port so existing sites are not stolen. Database is always isolated SQLite at `/opt/astra/data/astra.db` (never PostgreSQL).

Do not edit live nginx under `/etc/nginx/sites-available/astra` by hand — use `x-astra` or `astra-helper write-nginx`.
