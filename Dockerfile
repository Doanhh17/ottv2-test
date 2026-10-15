FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TMPDIR=/tmp

WORKDIR /app

RUN groupadd --system --gid 10001 ottv2 \
    && useradd --system --uid 10001 --gid ottv2 --home-dir /app --shell /usr/sbin/nologin ottv2 \
    && mkdir -p /app/storage/submissions /app/storage/replays /app/static/leaderboards /tmp \
    && chown -R 10001:10001 /app /tmp \
    && chmod 0700 /app/storage /app/storage/submissions /app/storage/replays

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 . .
RUN chmod 0755 /app \
    && chmod 0700 /app/storage /app/storage/submissions /app/storage/replays

USER 10001:10001
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=2)" || exit 1

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]