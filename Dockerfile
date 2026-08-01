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
    && mkdir -p /data/files \
    && chown app:app /data/files
WORKDIR /app
COPY --from=builder --chown=app:app /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "product_pdf_qr"]
