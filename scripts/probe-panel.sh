#!/usr/bin/env bash
# Astra — تشخیص نوع پنل و تست اتصال (سنایی / پاسارگاد)
# روی همان VPS که Astra نصب است اجرا کنید.
#
# سنایی (API Token از Settings → Security):
#   bash scripts/probe-panel.sh sanaei 'https://host:2053/webBasePath' 'YOUR_API_TOKEN'
#
# پاسارگاد (یوزر/پسورد ادمین یا اوپراتور):
#   bash scripts/probe-panel.sh pasarguard 'https://panel.example.com' 'admin' 'password'
#
set -euo pipefail

MODE="${1:-}"
BASE_RAW="${2:-}"
ARG3="${3:-}"
ARG4="${4:-}"

if [[ -z "$MODE" || -z "$BASE_RAW" || -z "$ARG3" ]]; then
  cat <<'USAGE'
Usage:
  bash scripts/probe-panel.sh sanaei     <BASE_URL> <API_TOKEN>
  bash scripts/probe-panel.sh pasarguard <BASE_URL> <USERNAME> <PASSWORD>

مثال سنایی:
  bash scripts/probe-panel.sh sanaei 'https://ir.example.com:2053/moj' 'xxxx-token-xxxx'

مثال پاسارگاد:
  bash scripts/probe-panel.sh pasarguard 'https://panel.example.com' 'admin' 'secret'
USAGE
  exit 1
fi

# نرمال‌سازی مثل Astra: حذف /panel /dashboard و …
normalize_base() {
  local u="$1"
  u="${u%/}"
  u="${u%/panel/inbounds}"
  u="${u%/panel/api-docs}"
  u="${u%/panel/settings}"
  u="${u%/dashboard}"
  u="${u%/admin}"
  u="${u%/panel}"
  u="${u%/ui}"
  u="${u%/app}"
  u="${u%/api/admin}"
  u="${u%/api}"
  echo "${u%/}"
}

BASE="$(normalize_base "$BASE_RAW")"
echo "=== Astra panel probe ==="
echo "mode:     $MODE"
echo "base_raw: $BASE_RAW"
echo "base:     $BASE"
echo

curl_probe() {
  local label="$1"
  local url="$2"
  shift 2
  echo "--- $label ---"
  echo "GET/POST $url"
  local tmp
  tmp="$(mktemp)"
  local code time_s
  # shellcheck disable=SC2068
  code="$(
    curl -sS -L --max-time 90 -o "$tmp" -w '%{http_code}' \
      "$@" "$url" 2>"${tmp}.err" || echo "000"
  )"
  time_s="$(
    curl -sS -L --max-time 90 -o /dev/null -w '%{time_total}' \
      "$@" "$url" 2>/dev/null || echo "?"
  )"
  local bytes
  bytes="$(wc -c <"$tmp" | tr -d ' ')"
  echo "HTTP $code · ${time_s}s · ${bytes} bytes"
  if [[ -s "${tmp}.err" ]]; then
    echo "curl_err: $(head -c 200 "${tmp}.err")"
  fi
  # پیش‌نمایش بدنه
  head -c 400 "$tmp" | tr '\n' ' '
  echo
  # اگر JSON باشد success / obj را خلاصه کن
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$tmp" <<'PY' || true
import json, sys
path = sys.argv[1]
raw = open(path, "rb").read()
try:
    data = json.loads(raw.decode("utf-8", "replace"))
except Exception as e:
    print("json: not valid (%s)" % e)
    raise SystemExit(0)
if isinstance(data, dict):
    print("json.keys:", ", ".join(list(data.keys())[:12]))
    if "success" in data:
        print("success:", data.get("success"), "msg:", str(data.get("msg") or data.get("message") or "")[:120])
    obj = data.get("obj")
    if isinstance(obj, list):
        print("obj: list len=%s" % len(obj))
        for i, row in enumerate(obj[:5]):
            if isinstance(row, dict):
                print("  [%s] id=%s protocol=%s remark=%s" % (
                    i, row.get("id"), row.get("protocol"), str(row.get("remark") or row.get("tag") or "")[:40]
                ))
    elif isinstance(obj, dict):
        print("obj: dict keys=", list(obj.keys())[:8])
    if "groups" in data and isinstance(data["groups"], list):
        print("groups: len=%s" % len(data["groups"]))
elif isinstance(data, list):
    print("json: list len=%s" % len(data))
else:
    print("json type:", type(data).__name__)
PY
  fi
  rm -f "$tmp" "${tmp}.err"
  echo
}

case "$MODE" in
  sanaei)
    TOKEN="$ARG3"
    TOKEN="${TOKEN#Bearer }"
    TOKEN="${TOKEN#bearer }"
    AUTH=( -H "Authorization: Bearer ${TOKEN}" -H "X-Requested-With: XMLHttpRequest" -H "Accept: application/json" )
    echo "fingerprint: آیا این سنایی است؟"
    curl_probe "sanaei slim"  "${BASE}/panel/api/inbounds/list/slim" "${AUTH[@]}"
    curl_probe "sanaei list"  "${BASE}/panel/api/inbounds/list" "${AUTH[@]}"
    curl_probe "sanaei status" "${BASE}/panel/api/server/status" "${AUTH[@]}"
    echo "اگر هر سه ۴۰۴/۴۰۱ بود، یا Token اشتباه است یا این آدرس سنایی نیست."
    echo "اگر slim=200 و list=timeout → پنل سنگین است؛ Astra باید با slim کار کند."
    ;;
  pasarguard|pg)
    USER="$ARG3"
    PASS="${ARG4:-}"
    if [[ -z "$PASS" ]]; then
      echo "برای پاسارگاد پسورد لازم است"
      exit 1
    fi
    TMP_TOK="$(mktemp)"
    echo "--- token ---"
    CODE="$(
      curl -sS -L --max-time 30 -o "$TMP_TOK" -w '%{http_code}' \
        -X POST "${BASE}/api/admin/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${USER}&password=${PASS}" || echo 000
    )"
    echo "HTTP $CODE"
    head -c 300 "$TMP_TOK"; echo
    BEARER="$(python3 - "$TMP_TOK" <<'PY' 2>/dev/null || true
import json,sys
d=json.load(open(sys.argv[1]))
print(d.get("access_token") or d.get("token") or "")
PY
)"
    rm -f "$TMP_TOK"
    if [[ -z "$BEARER" ]]; then
      echo "FAIL: توکن گرفته نشد — یوزر/پسورد یا آدرس اشتباه (یا پنل پاسارگاد نیست)."
      exit 2
    fi
    echo "token: OK (${#BEARER} chars)"
    AUTH=( -H "Authorization: Bearer ${BEARER}" -H "Accept: application/json" )
    curl_probe "GET /api/admin" "${BASE}/api/admin" "${AUTH[@]}"
    curl_probe "GET /api/groups/simple?all=true" "${BASE}/api/groups/simple?all=true" "${AUTH[@]}"
    curl_probe "GET /api/groups/simple" "${BASE}/api/groups/simple" "${AUTH[@]}"
    curl_probe "GET /api/groups" "${BASE}/api/groups?limit=100&offset=0" "${AUTH[@]}"
    echo "اگر token=OK ولی groups خطا داد → نقش اوپراتور بدون groups.read_simple یا نسخه API فرق دارد."
    ;;
  *)
    echo "mode نامعتبر: $MODE (sanaei | pasarguard)"
    exit 1
    ;;
esac

echo "=== done ==="
