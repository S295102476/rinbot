FROM python:3.11-slim-bookworm AS init
WORKDIR /app/qq-bot-py
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY qq-bot-py/config.example.yaml ./config.example.yaml
COPY qq-bot-py/tools/init_deployment.py ./tools/init_deployment.py
ENTRYPOINT ["python", "tools/init_deployment.py", "--match-owner"]

FROM node:22-bookworm-slim AS console
WORKDIR /build/console
COPY qq-bot-py/console/package.json qq-bot-py/console/package-lock.json ./
RUN npm ci
COPY qq-bot-py/console/ ./
RUN npm run build

# MinIO Community Edition stopped publishing a public Docker image. Build the
# pinned upstream source release instead of pulling an unresolvable registry
# tag. The release includes security fixes and declares Go 1.24.8.
FROM golang:1.24.8-bookworm AS minio-builder
ENV CGO_ENABLED=0 GO111MODULE=on
RUN go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM debian:bookworm-slim AS minio-runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=minio-builder /go/bin/minio /usr/local/bin/minio
RUN useradd --system --uid 1000 --home-dir /data minio \
    && mkdir -p /data && chown minio:minio /data
USER minio
VOLUME ["/data"]
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]

FROM python:3.11-slim-bookworm AS bot
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai
WORKDIR /app/qq-bot-py
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-noto-cjk fonts-noto-color-emoji tzdata ffmpeg libcairo2 \
    && rm -rf /var/lib/apt/lists/*
COPY qq-bot-py/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY qq-bot-py/ ./
COPY --from=console /build/console/dist ./console/dist
EXPOSE 8080
ENTRYPOINT ["python", "tools/container_entrypoint.py"]
