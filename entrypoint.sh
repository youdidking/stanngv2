#!/bin/bash

PORT="${PORT:-8000}"
export PANEL_PORT="${PANEL_PORT:-10000}"

# ==========================================
# ۱. حل مشکل دیتابیس (storage.py)
# ==========================================
export STANNG_DATA_DIR="/tmp/data"
export LOG_FILE_ENABLED=false

# ساخت پوشه‌های موقت
mkdir -p /tmp/data
mkdir -p /tmp/nginx/logs
mkdir -p /tmp/nginx/client_body_temp
mkdir -p /tmp/nginx/proxy_temp
mkdir -p /tmp/nginx/fastcgi_temp
mkdir -p /tmp/nginx/uwsgi_temp
mkdir -p /tmp/nginx/scgi_temp

# ==========================================
# ۲. حل مشکل Nginx (کپی کردن کانفیگ به /tmp)
# ==========================================
# کپی کردن فایل تنظیمات اصلی به پوشه قابل نوشتن
cp /etc/nginx/nginx.conf /tmp/nginx/nginx.conf

# تغییر پورت و مسیر لاگ‌ها در فایل کپی شده در /tmp (نه در /etc)
sed -i -E "s/listen [0-9]+;|listen NGINX_PORT;/listen ${PORT};/g" /tmp/nginx/nginx.conf
sed -i "s|^pid .*|pid /tmp/nginx/nginx.pid;|" /tmp/nginx/nginx.conf
sed -i "s|^error_log .*|error_log /tmp/nginx/logs/error.log warn;|" /tmp/nginx/nginx.conf

# تغییر مسیر فایل‌های موقت Nginx
sed -i "s|client_body_temp_path .*|client_body_temp_path /tmp/nginx/client_body_temp;|" /tmp/nginx/nginx.conf
sed -i "s|proxy_temp_path .*|proxy_temp_path /tmp/nginx/proxy_temp;|" /tmp/nginx/nginx.conf
sed -i "s|fastcgi_temp_path .*|fastcgi_temp_path /tmp/nginx/fastcgi_temp;|" /tmp/nginx/nginx.conf
sed -i "s|uwsgi_temp_path .*|uwsgi_temp_path /tmp/nginx/uwsgi_temp;|" /tmp/nginx/nginx.conf
sed -i "s|scgi_temp_path .*|scgi_temp_path /tmp/nginx/scgi_temp;|" /tmp/nginx/nginx.conf

# ==========================================
# ۳. اجرای Nginx با کانفیگ جدید و اجرای پایتون
# ==========================================
nginx -s stop 2>/dev/null || true
# به Nginx می‌گیم از فایل تنظیمات توی /tmp استفاده کن
nginx -c /tmp/nginx/nginx.conf

exec python3 main.py
