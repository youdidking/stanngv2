#!/bin/bash

PORT="${PORT:-8000}"
export PANEL_PORT="${PANEL_PORT:-10000}"

# ==========================================
# انتقال همه فایل‌های مهم به پوشه موقت /tmp
export LOG_FILE_ENABLED=false
export DB_PATH="/tmp/stanng.db"
export DATABASE_URL="sqlite:////tmp/stanng.db"
export DATA_DIR="/tmp/data"
# ==========================================

# ساخت پوشه‌های موقت مورد نیاز Nginx و برنامه
mkdir -p /tmp/nginx/logs /tmp/nginx/client_body_temp /tmp/nginx/proxy_temp /tmp/nginx/fastcgi_temp /tmp/nginx/uwsgi_temp /tmp/nginx/scgi_temp /tmp/data

# ==========================================
# تغییر مسیرهای Nginx به پوشه موقت تا ارور Read-only نده
# ==========================================
sed -i "s|^user .*|user root;|" /etc/nginx/nginx.conf
sed -i "s|^pid .*|pid /tmp/nginx/nginx.pid;|" /etc/nginx/nginx.conf
sed -i "s|^error_log .*|error_log /tmp/nginx/logs/error.log warn;|" /etc/nginx/nginx.conf
sed -i "s|client_body_temp_path .*|client_body_temp_path /tmp/nginx/client_body_temp;|" /etc/nginx/nginx.conf
sed -i "s|proxy_temp_path .*|proxy_temp_path /tmp/nginx/proxy_temp;|" /etc/nginx/nginx.conf
sed -i "s|fastcgi_temp_path .*|fastcgi_temp_path /tmp/nginx/fastcgi_temp;|" /etc/nginx/nginx.conf
sed -i "s|uwsgi_temp_path .*|uwsgi_temp_path /tmp/nginx/uwsgi_temp;|" /etc/nginx/nginx.conf
sed -i "s|scgi_temp_path .*|scgi_temp_path /tmp/nginx/scgi_temp;|" /etc/nginx/nginx.conf
sed -i -E "s/listen [0-9]+;|listen NGINX_PORT;/listen ${PORT};/g" /etc/nginx/nginx.conf

nginx -s stop 2>/dev/null || true
nginx

exec python3 main.py
