FROM python:3.12-slim

ARG CODEX_CLI_VERSION=0.144.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OTEL_SDK_DISABLED=true \
    CODEX_HOME=/codex

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        espeak-ng \
        fonts-liberation \
        fonts-unifont \
        gcc \
        git \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libfontconfig1 \
        libffi-dev \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libssl-dev \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        nodejs \
        npm \
        wget && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g "@openai/codex@${CODEX_CLI_VERSION}" && \
    codex --version

COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements.txt && \
    playwright install chromium

COPY . /app

RUN sed -i 's/\r$//' /app/docker/backend/entrypoint.sh && \
    chmod +x /app/docker/backend/entrypoint.sh

EXPOSE 8000

CMD ["/app/docker/backend/entrypoint.sh"]
