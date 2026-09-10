# Multi-arch (linux/amd64 + linux/arm64) by construction: pure-Python app,
# no node build step, wheels exist for every dependency on both arches.
# CI builds with buildx + QEMU; a single-arch image is a known past mistake
# on a fleet of 11 Raspberry Pis and one x86 box.
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing source does not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# Non-root. The app owns no files at runtime - all state is in S3.
RUN useradd --system --uid 10001 --no-create-home carpaccio
USER carpaccio

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "carpaccio.app:build", "--factory", "--host", "0.0.0.0", "--port", "8000"]
