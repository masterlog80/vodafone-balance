FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PROFILE_DIR=/data/profile \
    PORT=8080 \
    DISPLAY=:99

# Keep the GUI/browser stack required for one-time manual Vodafone login and SMS MFA.
# Chromium pulls in its own required shared-library dependencies, so avoid redundantly
# listing those packages explicitly. Drop apt caches and unused Chromium locale bundles
# in this same layer so they are not retained in the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium xvfb x11vnc novnc websockify ca-certificates fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /var/tmp/* \
    && if [ -d /usr/lib/chromium/locales ]; then \
         find /usr/lib/chromium/locales -maxdepth 1 -type f ! -name 'en-US.pak' -delete; \
       fi

LABEL org.opencontainers.image.title="Vodafone Balance" \
      org.opencontainers.image.description="Self-hosted Vodafone Italy prepaid SIM balance dashboard" \
      org.opencontainers.image.source="https://github.com/masterlog80/vodafone-balance" \
      org.opencontainers.image.url="https://github.com/masterlog80/vodafone-balance" \
      org.opencontainers.image.documentation="https://github.com/masterlog80/vodafone-balance" \
      org.opencontainers.image.version="1.3" \
      org.opencontainers.image.created="2026-10-25T00:00:00Z"

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --no-compile -r requirements.txt \
    && rm -rf /root/.cache/pip /tmp/* /var/tmp/*

COPY app.py .

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/profile \
    && chown -R 10001:10001 /app /data

USER 10001:10001
EXPOSE 8080 6080
CMD ["python", "/app/app.py"]
