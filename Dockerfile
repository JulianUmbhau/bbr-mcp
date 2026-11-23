# Runs the server over stdio. Listing the tools needs no key; set DATAFORDELER_API_KEY for the BBR tools.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && .venv/bin/bbr-mcp --self-test
USER nobody
ENTRYPOINT ["/app/.venv/bin/bbr-mcp"]
