# استقرار روی Railway (راهنمای تصویری/گام‌به‌گام)

## گزینه ۱: از روی مخزن گیت

1. پروژه را در یک مخزن (GitHub/GitLab) پوش کنید.
2. در Railway: **New Project → Deploy from GitHub repo** را انتخاب کنید.
3. در تب **Variables** مقدارهای زیر را اضافه کنید:

| Variable | مقدار |
| --- | --- |
| `BOT_TOKEN` | توکن از @BotFather (اجباری) |
| `OWNER_ID` | شناسه عددی شما (اجباری) |
| `ENVIRONMENT` | `production` |
| `LOG_LEVEL` | `INFO` |
| `TIMEZONE` | `Asia/Tehran` |
| `USE_WEBHOOK` | `false` (برای Polling) یا `true` برای وب‌هوک |
| `WEBHOOK_URL` | فقط در حالت وب‌هوک: دامنه عمومی Railway |
| `DATABASE_URL` | اختیاری (در صورت استفاده از Postgres) |

4. در تب **Settings → Volumes** یک Volume با Mount Path `/app/data` اضافه کنید
   تا فایل SQLite پایدار بماند.
5. Deploy را اجرا کنید؛ در تب **Deployments → Logs** باید عبارت آماده‌به‌کار را ببینید.

## گزینه ۲: Railway CLI

```bash
railway login
railway init            # انتخاب/ساخت پروژه
railway variables set BOT_TOKEN=...:... OWNER_ID=123456789
railway up              # آپلود و استقرار
railway logs            # مشاهده لاگ
```

## Health-check

اگر `USE_WEBHOOK=true` باشد، سرور داخلی روی پورت `WEBAPP_PORT` (پیش‌فرض ۸۰۸۰) بالا می‌آید
و مسیر `/health` پاسخ می‌دهد. در غیر این صورت نیازی به Health-check نیست (Polling).

## نکات عملیاتی

- بعد از هر Deploy، در صورت تنظیم `DROP_PENDING_UPDATES=true` پیام‌های قدیمی نادیده گرفته می‌شوند.
- برای جلوگیری از دو instance همزمان در Polling (تلگرام اجازهٔ دو getUpdates همزمان نمی‌دهد)،
  تعداد Replicaها را روی ۱ نگه دارید یا از وب‌هوک استفاده کنید.
- پشتیبان‌گیری: دستور `بکاپ` در گروه، فایل JSON می‌سازد؛ برای بازیابی از `بازیابی` استفاده کنید.
