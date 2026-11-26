# syntax=docker/dockerfile:1
# Google Analytics 4 : serveur MCP officiel de Google (PyPI analytics-mcp),
# lancé en stdio avec les identifiants "authorized_user" partagés, et exposé
# en Streamable HTTP par le multiplexeur services/mux sur /ga4/mcp :
# quelques processus analytics-mcp partagés par toutes les sessions.
# Image linux/arm64 uniquement.

# --- multiplexeur MCP (Node) -------------------------------------------------
FROM node:24-trixie-slim AS mux
WORKDIR /opt/mux
COPY services/mux/package.json services/mux/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY services/mux/mux.mjs ./

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

COPY --from=mux /usr/local/bin/node /usr/local/bin/node
COPY --from=mux /opt/mux /opt/mux

COPY services/ga4/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --chmod=755 services/common/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["node", "/opt/mux/mux.mjs", \
     "--port", "8000", \
     "--path", "/ga4/mcp", \
     "--health", "/ga4/healthz", \
     "--", "/opt/venv/bin/analytics-mcp"]
