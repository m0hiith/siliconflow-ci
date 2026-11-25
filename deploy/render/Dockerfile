# SiliconFlow CI on Render's free tier, in demo mode (docs/render.md).
#
# One container runs what Compose runs as separate services: nginx serves the
# built UI and proxies /api to the API on 127.0.0.1:8000, beside the worker.
# Render's free tier has no persistent disk and no background workers, and
# the API and worker share the artifact store, so they share a container.
# Build context: the repository root (render.yaml sets dockerContext: .).

FROM node:24-alpine@sha256:44fce696f38ba8c987c4db89954051000bbd05ffd8d8659ba15ed4a8602e937f AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm test && npm run build

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    PORT=10000

RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx \
 && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled /etc/nginx/conf.d

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
# Demo scenarios and fixtures (Compose mounts ./demo here instead).
COPY demo /demo
COPY --from=frontend /app/dist /srv/www
COPY deploy/render/nginx.conf /etc/sfci/nginx.conf.template
COPY deploy/render/start.sh deploy/render/reset_demo_db.py /app/deploy/

RUN useradd --system --uid 10001 --no-create-home sfci \
 && mkdir -p /var/lib/sfci/artifacts \
 && chown sfci /var/lib/sfci/artifacts \
 && chmod 0755 /app/deploy/start.sh
USER sfci

EXPOSE 10000
CMD ["/app/deploy/start.sh"]
