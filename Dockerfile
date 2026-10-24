FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DENO_DIR=/tmp/deno-dir
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
# Bump this value to force a fresh yt-dlp install when the upstream extractor changes.
ARG YTDLP_REFRESH=2026-10-22
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --pre --upgrade --force-reinstall "yt-dlp[default]" \
    && yt-dlp --version \
    && deno --version
COPY app.py .
COPY telegram_bot.py .
COPY static ./static
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
