# Armando Group Manager - production image (Railway / any container host)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Asia/Tehran is the bot default timezone
RUN ln -sf /usr/share/zoneinfo/Asia/Tehran /etc/localtime \
 && echo "Asia/Tehran" > /etc/timezone

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/logs && useradd -m -u 10001 armando \
 && chown -R armando:armando /app
USER armando

VOLUME ["/app/data"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${WEBAPP_PORT:-8080}/health', timeout=5)" || exit 1

CMD ["python", "-m", "app.main"]
