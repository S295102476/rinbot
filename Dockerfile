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
