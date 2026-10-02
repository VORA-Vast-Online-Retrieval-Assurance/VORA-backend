# VORA backend: FastAPI + Playwright Chromium + a private SearXNG, in one container (Hugging Face Docker Space ready).
# Build: docker build -t vora .        Run: docker run -p 7860:7860 --env-file .env vora
#
# The Playwright image carries Chromium and its system libraries for the exact Playwright version VORA pins.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/vora \
    VORA_HOST=0.0.0.0 \
    VORA_PORT=7860 \
    VORA_HEADLESS=true \
    VORA_STATE_DIR=/home/vora/state \
    DATABASE_PATH=/home/vora/state/vora.db \
    VORA_SEARXNG_URL=http://127.0.0.1:8080 \
    SEARXNG_SETTINGS_PATH=/opt/searxng/settings.yml

# SearXNG from source in its own virtual environment, so its libraries never clash with VORA's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/searxng/searxng.git /opt/searxng/src \
    && python3 -m venv /opt/searxng/venv \
    && /opt/searxng/venv/bin/pip install -r /opt/searxng/src/requirements.txt \
    && rm -rf /opt/searxng/src/.git
COPY deploy/searxng/settings.yml /opt/searxng/settings.yml

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Hugging Face runs containers as user 1000; everything VORA writes lives under its home.
RUN (id -u 1000 >/dev/null 2>&1 || useradd -m -u 1000 vora) \
    && mkdir -p /home/vora/state /app/data \
    && chown -R 1000:1000 /home/vora /app/data /opt/searxng \
    && chmod +x /app/deploy/start.sh
USER 1000

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=8).status == 200 else 1)"

CMD ["/app/deploy/start.sh"]
