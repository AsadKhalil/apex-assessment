FROM python:3.12-slim
# uv binary; the tag matches the version used to produce uv.lock (fall back to :latest if the tag is unavailable)
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DB_PATH=/app/data/assistant.db KB_DIR=/app/kb
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY kb ./kb
RUN useradd -m -u 1000 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
