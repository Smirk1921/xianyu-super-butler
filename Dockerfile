FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DOCKER_ENV=true \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

FROM node:24-alpine AS frontend-builder

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN VITE_OUT_DIR=dist npm run build

FROM base AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade \
        "certifi>=2026.10.19" \
        "aiohttp>=3.14.4" \
        "Pillow>=13.0.0" \
        "protobuf>=5.29.6"

COPY . .
COPY --from=frontend-builder /frontend/dist ./static

FROM base AS runtime

LABEL maintainer="23Star" \
      version="3.0.0-beta" \
      description="Xianyu account, item, order, reply, and delivery management" \
      repository="https://github.com/23Star/xianyu-super-butler" \
      author="23Star"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        curl \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
        libgl1 \
        libglib2.0-0 \
        xvfb \
        x11-utils \
        xz-utils && \
    rm -rf /var/lib/apt/lists/*

# 2026-10 安全更新：用官方 Node 24.22.1 替换 Debian 自带的 EOL Node 18
ARG NODE_VERSION=24.22.1
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm -f /tmp/node.tar.xz && node -v && npm -v

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN playwright install-deps chromium && \
    playwright install chromium && \
    mkdir -p /app/logs /app/data /app/backups /app/static/uploads/images && \
    chmod 777 /app/logs /app/data /app/backups /app/static/uploads /app/static/uploads/images && \
    chmod +x /app/entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["/app/entrypoint.sh"]
