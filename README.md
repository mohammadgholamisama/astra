# Astra — پنل چندمنبعی V2Ray

پنل مدیریت و فروش کانفیگ با پشتیبانی همزمان از **سنایی (3x-ui)**، **پاسارگاد (PasarGuard)**، **مرزبان** و **ربکا**.

- API بیرونی سازگار با پاسارگاد برای اتصال ربات‌ها
- گروه‌بندی چند پنل · ساب‌لینک تجمیعی · صفحه HTML
- پیش‌فرض سرور: **SQLite** (بدون نیاز به PostgreSQL)

---

## نصب روی سرور لینوکس (Ubuntu / Debian)

یک دستور — مثل 3x-ui / PasarGuard:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/mohammadgholamisama/astra.git/main/install.sh)
```

یا از کلون محلی:

```bash
git clone https://github.com/mohammadgholamisama/astra.git
cd astra
sudo bash install.sh
```

نصب‌کننده به‌صورت خودکار:

- پکیج‌ها، Node.js 20، nginx، certbot، Python venv را نصب می‌کند
- پروژه را در `/opt/astra` قرار می‌دهد و کاربر سیستمی `astra` می‌سازد
- یک **پورت آزاد** برای uvicorn انتخاب می‌کند (ترتیب: 2086 → 2087 → 8000 → 8080 …)
- پورت **80** را برای nginx بررسی می‌کند (اگر مشغول باشد هشدار می‌دهد)
- فرانت را با `VITE_API_BASE` خالی (same-origin) بیلد می‌کند
- سرویس `astra` + سایت nginx را فعال می‌کند
- منوی مدیریت `x-astra` را نصب می‌کند

بعد از نصب، پنل روی **IP سرور** در دسترس است:

```text
http://YOUR_SERVER_IP
```

دامنه و SSL را با منو تنظیم کنید:

```bash
x-astra
```

گزینه‌ها: وضعیت، تغییر پورت، دامنه پنل، دامنه ساب، صدور/تمدید SSL، ری‌استارت، آپدیت از git.

---

## توسعه محلی (Windows / dev)

```powershell
# Backend
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (ترمینال دیگر)
cd frontend
npm install
npm run dev
```

نمونه env: `.env.example`  
ورود پیش‌فرض لوکال: `admin` / `admin123`

- پنل: http://localhost:5173  
- API Docs: http://localhost:8000/docs  

اختیاری PostgreSQL:

```
DATABASE_URL=postgresql+asyncpg://astra:astra_secret@localhost:5432/astra
```

---

## ساختار

```
install.sh          نصب یک‌مرحله‌ای سرور
deploy/             systemd · nginx · x-astra · astra-helper
backend/            FastAPI + adapters
frontend/           React admin (RTL)
```

## لایسنس

MIT — © 2026 mohammadgholamisama
