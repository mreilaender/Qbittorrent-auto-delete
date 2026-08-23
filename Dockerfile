# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /build
COPY . .
RUN pip install --no-cache-dir --prefix=/install .

# ---- runtime ----
FROM python:3.12-slim
WORKDIR /app

# Install package (no -e, no editable mode)
COPY --from=builder /install /usr/local

# State & config directory
ENV STATE_DIR=/app
ENV CONFIG_PATH=/app/config.ini
ENV CLEANUP_INTERVAL=1

# Config and log files live in a single mounted volume
VOLUME ["/app"]

# Healthcheck: runs cleanup in --test mode every 5 minutes
HEALTHCHECK --interval=5m --timeout=30s --retries=3 --start-period=2m \
  CMD qbittorrent-cleanup --config /app/config.ini --test || exit 1

# Default: run the scheduler (both jobs in one container)
ENTRYPOINT ["qbittorrent-auto-delete-scheduler"]
CMD []
