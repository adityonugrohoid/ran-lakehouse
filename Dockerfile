# The lakehouse app (rule S3): generator, collector, pipeline and API in one
# image. compose.yaml runs it as the `app` service (python -m
# ran_lakehouse.bootstrap).
FROM python:3.12.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY transform ./transform
COPY contract ./contract
RUN uv sync --frozen --no-dev

# DuckDB's extensions are part of the image, so the container needs no
# download at run time.
RUN python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL iceberg; INSTALL httpfs')"

CMD ["python", "-m", "ran_lakehouse.bootstrap"]
