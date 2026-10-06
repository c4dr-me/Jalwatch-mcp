FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    JALWATCH_ENVIRONMENT=deployed
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN useradd --create-home --uid 10001 jalwatch && mkdir -p /app/data && chown -R jalwatch:jalwatch /app/data
USER jalwatch
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn jalwatch.api.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
