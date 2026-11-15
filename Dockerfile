# Codewright as a shared MCP server for a team (Streamable HTTP at /mcp).
#   docker build -t codewright .
#   docker run -p 8765:8765 -e CODEWRIGHT_MCP_TOKENS=$(codewright mcp token) codewright
# Serve repositories already on the host: -v /srv/repos/shop:/repos/shop  and append  --root /repos/shop
# Private git repos: -e CODEWRIGHT_GIT_TOKEN=...   Your own package index: -e CODEWRIGHT_INDEX_URL=...
# Build through Artifactory/Nexus: docker build --build-arg PIP_INDEX_URL=https://.../simple -t codewright .
FROM python:3.12-slim
ARG PIP_INDEX_URL

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 codewright

USER codewright
ENV CODEWRIGHT_HOME=/home/codewright/.codewright \
    PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["codewright", "mcp", "--http", "--host", "0.0.0.0", "--port", "8765"]
