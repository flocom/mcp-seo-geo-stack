# syntax=docker/dockerfile:1
# Passerelle d'authentification devant gsc-bing et ga4 : vérifie le jeton Cloudflare Access
# (signature, émetteur, audience, expiration) puis relaie la requête en streaming.
# Bibliothèque standard Python uniquement.
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY services/common/access_jwt.py /app/access_jwt.py
COPY services/gate/gate.py /app/gate.py

USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/gate/healthz', timeout=3).status == 200 else 1)"]
CMD ["python", "/app/gate.py"]
