# syntax=docker/dockerfile:1.7
# ---- build stage: official uv image, resolve deps from the lockfile ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---- runtime stage: slim, non-root ----
FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# outbox/ (emails/messages when no SMTP/Telegram is configured) is not in the image
# (.dockerignore): create it writable for the non-root user
RUN mkdir -p /app/outbox && chown app:app /app/outbox
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn finpilot.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
