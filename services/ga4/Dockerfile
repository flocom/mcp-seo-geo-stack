# syntax=docker/dockerfile:1
# Google Analytics 4 : serveur MCP officiel de Google (PyPI analytics-mcp),
# lancé en stdio avec les identifiants "authorized_user" partagés, et exposé
# en Streamable HTTP par supergateway sur /ga4/mcp.
# Image linux/arm64 uniquement.

# --- supergateway (Node) -----------------------------------------------------
FROM node:24-trixie-slim AS gateway
WORKDIR /opt/gateway
COPY services/ga4/package.json services/ga4/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

# --- analytics-mcp (Python) --------------------------------------------------
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    DATA_DIR=/data \
    REQUIRES=google \
    PATH=/opt/venv/bin:$PATH

# libstdc++ est nécessaire au binaire node copié depuis l'étape précédente.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gateway /usr/local/bin/node /usr/local/bin/node
COPY --from=gateway /opt/gateway /opt/gateway

COPY services/ga4/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chmod=755 services/common/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["node", "/opt/gateway/node_modules/supergateway/dist/index.js", \
     "--stdio", "/opt/venv/bin/analytics-mcp", \
     "--outputTransport", "streamableHttp", \
     "--stateful", "--sessionTimeout", "600000", \
     "--port", "8000", \
     "--streamableHttpPath", "/ga4/mcp", \
     "--healthEndpoint", "/ga4/healthz", \
     "--logLevel", "none"]
