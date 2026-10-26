# Astra Mobile Ping

اپ اندروید (`client/`) + هاب Go (`backend-go/`) برای پینگ واقعی چند دستگاه و ترتیب خودکار کانفیگ‌ها در پنل.

## روی سرور پنل

با `astra-helper apply-update` سرویس `astra-ping-hub` هم بیلد/ری‌استارت می‌شود:

- گوش می‌دهد روی `127.0.0.1:18080`
- از طریق nginx پنل: `https://<ASTRA_PANEL_DOMAIN>/ping-api/`
- هر ۱۵ دقیقه میانگین پینگ‌ها را به `POST /api/admin/config-order/ping-rank` می‌فرستد
- `.env` پنل و `data/` دست‌نخورده می‌مانند؛ env هاب جدا در `mobile-app/backend-go/.env` است

## توسعه لوکال

```bash
cd backend-go && docker compose up --build
cd client && npm i && npx react-native run-android
```
