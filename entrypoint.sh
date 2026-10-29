#!/bin/bash

# Set default port if not provided by Railway / Render
PORT="${PORT:-8000}"

# Panel internal port
export PANEL_PORT="${PANEL_PORT:-10000}"

# Update nginx.conf port dynamically (supports both template and repeated runs)
sed -i -E "s/listen [0-9]+;|listen NGINX_PORT;/listen ${PORT};/g" /etc/nginx/nginx.conf

# Stop any previous Nginx instance if running, then start fresh
nginx -s stop 2>/dev/null || true
nginx

# Start Python Panel in foreground
exec python3 main.py
