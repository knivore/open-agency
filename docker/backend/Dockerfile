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
        ffmpeg \
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
        libavcodec-dev \
        libavdevice-dev \
        libavfilter-dev \
        libavformat-dev \
        libavutil-dev \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libssl-dev \
        libswresample-dev \
        libswscale-dev \
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
        pkg-config \
        wget && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g "@openai/codex@${CODEX_CLI_VERSION}" && \
    codex --version

COPY requirements.txt /tmp/requirements.txt

RUN python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements.txt && \
    playwright install chromium

ARG OPENVOICE_COMMIT=74a1d147b17a8c3092dd5430504bd83ef6c7eb23

# OpenVoice V1 pins Python-era dependencies that are incompatible with the
# backend's Python 3.12 runtime. Keep its trusted model runner isolated in a
# Python 3.10 environment and pin the audited upstream source revision.
COPY docker/backend/openvoice-requirements.txt /tmp/openvoice-requirements.txt
# Import the public API during the build so legacy dependency incompatibilities fail early.
RUN git clone https://github.com/myshell-ai/OpenVoice.git /opt/openvoice && \
    git -C /opt/openvoice checkout "${OPENVOICE_COMMIT}" && \
    uv venv --python 3.10 /opt/openvoice/.venv && \
    uv pip install --python /opt/openvoice/.venv/bin/python torch==2.13.0 && \
    uv pip install --python /opt/openvoice/.venv/bin/python --no-deps -e /opt/openvoice && \
    uv pip install --python /opt/openvoice/.venv/bin/python -r /tmp/openvoice-requirements.txt && \
    /opt/openvoice/.venv/bin/python -c "from openvoice.api import BaseSpeakerTTS, ToneColorConverter"

COPY . /app

RUN sed -i 's/\r$//' /app/docker/backend/entrypoint.sh && \
    chmod +x /app/docker/backend/entrypoint.sh

EXPOSE 8000

CMD ["/app/docker/backend/entrypoint.sh"]
