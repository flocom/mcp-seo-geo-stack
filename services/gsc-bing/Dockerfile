# syntax=docker/dockerfile:1
# Google Search Console + Bing Webmaster Tools (paquet npm search-console-mcp),
# exposé en Streamable HTTP par le multiplexeur services/mux sur /gsc-bing/mcp :
# quelques processus search-console-mcp partagés par toutes les sessions.
# Image linux/arm64 uniquement (construite nativement sur l'Oracle ou un runner ARM).
FROM node:24-trixie-slim

ENV NODE_ENV=production \
    HOME=/tmp \
    DATA_DIR=/data \
    REQUIRES=google-or-bing

WORKDIR /app

# Versions fixées dans package.json + package-lock.json (mises à jour par Renovate).
COPY services/gsc-bing/package.json services/gsc-bing/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force

WORKDIR /opt/mux
COPY services/mux/package.json services/mux/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force
COPY services/mux/mux.mjs ./
WORKDIR /app

COPY --chmod=755 services/common/entrypoint.sh /usr/local/bin/entrypoint.sh

# /data appartient à l'utilisateur non root : un volume nommé neuf hérite de ces droits.
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["node", "/opt/mux/mux.mjs", \
     "--port", "8000", \
     "--path", "/gsc-bing/mcp", \
     "--health", "/gsc-bing/healthz", \
     "--", "node", "/app/node_modules/search-console-mcp/dist/index.js"]
