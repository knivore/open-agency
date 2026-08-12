FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BROWSER_RUNTIME_ROOT=/var/lib/open-agency-browser \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        fonts-unifont \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
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
        wget && \
    rm -rf /var/lib/apt/lists/*

COPY docker/browser-runtime/requirements.txt /tmp/browser-runtime-requirements.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/browser-runtime-requirements.txt && \
    python -m patchright install chromium

RUN useradd --create-home --uid 10001 browser && \
    mkdir -p /var/lib/open-agency-browser /ms-playwright && \
    chown -R browser:browser /var/lib/open-agency-browser /ms-playwright /home/browser

COPY app/__init__.py /app/app/__init__.py
COPY app/browser_runtime /app/app/browser_runtime

USER browser

EXPOSE 8010

CMD ["uvicorn", "app.browser_runtime.server:app", "--host", "0.0.0.0", "--port", "8010", "--no-access-log"]
