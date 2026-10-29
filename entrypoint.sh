#!/bin/bash

export STANNG_DATA_DIR="/tmp/data"
export XRAY_CONFIG_PATH="/tmp/config.json"
export LOG_FILE_ENABLED=false

# دریافت پورت عمومی از Hostless
export PORT="${PORT:-8000}"

# ساخت پوشه‌های موقت Nginx در /tmp
mkdir -p /tmp/nginx/logs /tmp/nginx/client_body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi

# کپی کردن کانفیگ به /tmp و تغییر پورت NGINX_PORT به پورت Hostless
cp /app/nginx.conf /tmp/nginx.conf
sed -i "s/NGINX_PORT/${PORT}/g" /tmp/nginx.conf

# اجرای Nginx در پس‌زمینه با کانفیگ جدید
nginx -c /tmp/nginx.conf

# اجرای پنل وب روی پورت 10000 (که Nginx ترافیک رو بهش میده)
exec uvicorn main:app --host 127.0.0.1 --port 10000
