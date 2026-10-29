#!/bin/bash

PORT="${PORT:-8000}"
export PANEL_PORT="${PANEL_PORT:-10000}"

# ساخت پوشه‌های موقت در /tmp
mkdir -p /tmp/nginx /tmp/logs /tmp/data /tmp/nginx/client_body_temp /tmp/nginx/proxy_temp

# کپی کردن فایل تنظیمات Nginx به /tmp تا بتونیم ویرایشش کنیم
cp /etc/nginx/nginx.conf /tmp/nginx/nginx.conf

# تغییر پورت و مسیر لاگ‌ها در فایل کپی شده در /tmp
sed -i -E "s/listen [0-9]+;|listen NGINX_PORT;/listen ${PORT};/g" /tmp/nginx/nginx.conf
sed -i "s|^pid .*|pid /tmp/nginx/nginx.pid;|" /tmp/nginx/nginx.conf
sed -i "s|^error_log .*|error_log /tmp/nginx/error.log warn;|" /tmp/nginx/nginx.conf

# اجرای Nginx با فایل تنظیمات جدید در /tmp
nginx -c /tmp/nginx/nginx.conf

# اجرای برنامه پایتون
exec python3 main.py
