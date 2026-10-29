#!/bin/bash

# تنظیم پورت برای Hostless و برنامه پایتون
export PORT="${PORT:-8000}"
export PANEL_PORT="${PORT}"

# انتقال داده‌های پایدار به پوشه موقت
export STANNG_DATA_DIR="/tmp/data"
export XRAY_CONFIG_PATH="/tmp/config.json"
export LOG_FILE_ENABLED=false

# اجرای مستقیم برنامه پایتون و گوش دادن روی تمام اینترفیس‌ها
exec python3 main.py
