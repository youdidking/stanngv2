#!/bin/bash

# انتقال داده‌های پایدار به پوشه موقت
export STANNG_DATA_DIR="/tmp/data"
export XRAY_CONFIG_PATH="/tmp/config.json"
export LOG_FILE_ENABLED=false

# اجرای برنامه با پورت تعیین‌شده توسط Hostless
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
