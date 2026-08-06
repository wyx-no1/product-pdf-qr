# syntax=docker/dockerfile:1.7

FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS builder

ARG UV_VERSION=0.12.1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS development

ARG UV_VERSION=0.12.1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN addgroup -g 10001 -S app \
    && adduser -u 10001 -S -D -G app -h /home/app -s /sbin/nologin app \
    && pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /workspace
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --all-groups --no-editable
COPY --chown=app:app alembic.ini Makefile ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app tests ./tests
USER 10001:10001


FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    APP_BIND_HOST=127.0.0.1 \
    APP_PORT=8000

RUN addgroup -g 10001 -S app \
    && adduser -u 10001 -S -D -G app -h /home/app -s /sbin/nologin app \
    && mkdir -p /data/files /var/log/product-pdf-qr \
    && chown app:app /data/files /var/log/product-pdf-qr
WORKDIR /app
COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
RUN find /opt/venv /app -type d -name __pycache__ -prune -exec rm -rf {} +
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "product_pdf_qr"]


# The official image's gosu binary is only used when its entrypoint starts as root.
# Production fixes UID 70, so removing that unreachable privilege-drop helper also
# removes its separately compiled Go runtime from the final attack surface.
FROM postgres:16.14-alpine3.24@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777 AS database-runtime

USER 0:0
RUN unlink /usr/local/bin/gosu
USER 70:70


FROM nginxinc/nginx-unprivileged:1.29.4-alpine@sha256:a6c4f61f456b85b8fdf7ec7ab28cc3e299440e6fb4a9dea520e5fd8fd440025e AS proxy-runtime

USER 0:0
RUN apk upgrade --no-cache
USER 101:101


FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS certbot-runtime

COPY deploy/production/certbot/requirements.txt /tmp/certbot-requirements.txt
RUN apk add --no-cache openssl=3.5.7-r0 \
    && pip install --no-cache-dir --requirement /tmp/certbot-requirements.txt \
    && unlink /tmp/certbot-requirements.txt
USER 1000:101
ENTRYPOINT ["certbot"]
