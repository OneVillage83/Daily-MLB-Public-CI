# Docker Hub index digest for python:3.12-slim (3.12.13-slim, resolved 2026-07-12 PT).
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="Daily MLB Phase 1 Collector" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --system mlb \
    && useradd --system --gid mlb --home-dir /app --shell /usr/sbin/nologin mlb

COPY app ./app
COPY scripts/initialize_database.py ./scripts/initialize_database.py
COPY docs ./docs
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN mkdir -p /data/artifacts \
    && chown -R root:root /app \
    && chmod -R a-w /app \
    && chmod 0555 /app/docker-entrypoint.sh \
    && chown -R mlb:mlb /data \
    && chmod 0750 /data /data/artifacts

USER mlb

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
