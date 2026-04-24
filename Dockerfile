# Cloud Run runtime image. Multi-stage would shave ~100 MB but 5f's clock
# doesn't care — this is simpler and builds in <3 min on Cloud Build.
FROM python:3.11-slim

# uv is the package manager; grab the official single-binary install.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Unbuffered stdout so uvicorn logs stream into Cloud Run Logs.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    PORT=8080

WORKDIR /app

# Install deps first so the layer caches across code-only rebuilds.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the app itself.
COPY main.py ./
COPY models ./models
COPY pipeline ./pipeline
COPY scripts ./scripts
COPY static ./static

# uv sync creates a .venv; put it on PATH so `uvicorn` is found.
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

# Cloud Run sets $PORT (default 8080). Shell-form CMD expands it.
CMD exec uvicorn main:app --host 0.0.0.0 --port "${PORT}"
