FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROFILE_DIR=/data/profile \
    PORT=8080 \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc websockify ca-certificates fonts-liberation \
      libnss3 libatk-bridge2.0-0 libxkbcommon0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libasound2 libatspi2.0-0 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/profile \
    && chown -R 10001:10001 /app /data

USER 10001:10001
EXPOSE 8080 6080
CMD ["python", "/app/app.py"]
