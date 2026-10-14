# Deploy assets

Files used by `install.sh` on Ubuntu/Debian servers:

| File | Purpose |
|------|---------|
| `astra.service` | systemd unit (`User=astra`, uvicorn on `ASTRA_APP_PORT`) |
| `nginx-http.conf.tpl` | HTTP template — placeholders `__ASTRA_PORT__` |
| `nginx-ssl.conf.tpl` | HTTPS template — `__DOMAIN__`, `__ASTRA_PORT__` |
| `x-astra` | Interactive menu (`/usr/local/bin/x-astra`) |
| `astra-helper` | Root helper for nginx/SSL/env (`/usr/local/bin/astra-helper`) |
| `sudoers.astra` | Passwordless sudo for `astra` → `astra-helper` only |

Do not edit live nginx under `/etc/nginx/sites-available/astra` by hand — use `x-astra` or `astra-helper write-nginx`.
