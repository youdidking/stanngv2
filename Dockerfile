FROM alpine:3.20

# Install runtime dependencies
RUN apk add --no-cache python3 py3-pip nginx curl unzip jq bash tzdata

# Install build dependencies needed for Pillow and psutil
RUN apk add --no-cache --virtual .build-deps \
    gcc musl-dev python3-dev zlib-dev jpeg-dev freetype-dev linux-headers

WORKDIR /app
COPY requirements.txt .

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt \
    && apk del .build-deps

# Install Xray-core
RUN curl -fsSL -o xray.zip "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip" && \
    unzip xray.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/xray && \
    rm xray.zip

# Copy application files
COPY . /app

# Copy nginx config
COPY nginx.conf /etc/nginx/nginx.conf

# Entrypoint setup
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
