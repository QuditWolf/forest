# forest server: web UIs + REST API + MCP (/mcp) + git sync (/git)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*'
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml
COPY forest ./forest
COPY static ./static
RUN printf '#!/bin/sh\nexec python -m forest.cli "$@"\n' > /usr/local/bin/forest && chmod +x /usr/local/bin/forest \
 && useradd -u 1000 -m forest && mkdir -p /data && chown forest:forest /data

ENV FOREST_DATA=/data \
    FOREST_HOST=0.0.0.0 \
    FOREST_PORT=7000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER forest
# No VOLUME on purpose: mount host directories with -v (see docker-compose.yml)
EXPOSE 7000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7000/healthz', timeout=4)"

ENTRYPOINT ["tini", "--"]
# FORWARDED_ALLOW_IPS (env) tells uvicorn which proxy may set X-Forwarded-For (your nginx VPN IP)
CMD ["python", "-m", "uvicorn", "forest.api:app", "--host", "0.0.0.0", "--port", "7000", "--proxy-headers"]
