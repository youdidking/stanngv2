#!/bin/bash

echo "=== Starting Entrypoint ==="
export STANNG_DATA_DIR="/tmp/data"
export XRAY_CONFIG_PATH="/tmp/config.json"
export LOG_FILE_ENABLED=false
export PORT="${PORT:-8000}"

echo "Hostless PORT is: $PORT"

# ساخت پوشه‌های Nginx در /tmp
mkdir -p /tmp/nginx/logs /tmp/nginx/client_body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi

# کپی و اصلاح کانفیگ Nginx
cp /app/nginx.conf /tmp/nginx.conf
sed -i "s/NGINX_PORT/${PORT}/g" /tmp/nginx.conf

echo "=== Starting Nginx ==="
nginx -c /tmp/nginx.conf
if [ $? -eq 0 ]; then
    echo "✅ Nginx started successfully on port $PORT"
else
    echo "❌ Nginx FAILED to start!"
fi

echo "=== Starting Python Panel ==="
# پایتون روی پورت 10000 اجرا میشه تا Nginx ترافیک رو بهش بفرسته
exec uvicorn main:app --host 127.0.0.1 --port 10000
