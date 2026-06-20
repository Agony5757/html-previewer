# Self-contained preview hosting service (API + static), stdlib Python only.
# Build context is the repository root:
#   docker build -f deploy/docker/Dockerfile -t html-previewer .
FROM python:3.12-slim

# git is only needed if you enable append-only archiving (PREVIEW_GIT_ENABLED=1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/preview_common.py server/preview_archive.py \
     server/preview_api.py server/reaper.py /app/
COPY deploy/docker/entrypoint.sh /app/entrypoint.sh
COPY deploy/__notfound.html /app/static/__notfound.html
COPY deploy/admin.html /app/admin.html
RUN chmod +x /app/entrypoint.sh

# Self-contained defaults (the container serves static content itself).
ENV PREVIEW_SERVE_STATIC=1 \
    PREVIEW_BIND=0.0.0.0 \
    PREVIEW_PORT=8787 \
    PREVIEW_PUBLIC_HOST=localhost:8787 \
    PREVIEW_PUBLIC_SCHEME=http \
    PREVIEW_LIVE_ROOT=/data/live \
    PREVIEW_STATE_DIR=/data/state \
    PREVIEW_STATIC_DIR=/app/static \
    PREVIEW_DEFAULT_DAYS=7 \
    PREVIEW_MAX_DAYS=30

EXPOSE 8787
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
url='http://127.0.0.1:%s/_api/health'%os.environ.get('PREVIEW_PORT','8787'); \
sys.exit(0 if urllib.request.urlopen(url,timeout=3).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
