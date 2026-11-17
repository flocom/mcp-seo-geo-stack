# syntax=docker/dockerfile:1
# Page /setup : saisie du client OAuth Google et de la clé Bing, connexion
# Google dans le navigateur via le tunnel. Bibliothèque standard Python uniquement.
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY services/setup/app.py /app/app.py

RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/setup/healthz', timeout=3).status == 200 else 1)"]
CMD ["python", "/app/app.py"]
