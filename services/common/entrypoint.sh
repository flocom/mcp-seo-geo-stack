#!/bin/sh
# Lanceur commun aux services MCP (gsc-bing, ga4).
#
# 1. Attend que la page /setup ait écrit les identifiants dans le volume /data.
# 2. Exporte GOOGLE_APPLICATION_CREDENTIALS (fichier "authorized_user") et
#    BING_API_KEY, que lisent search-console-mcp et analytics-mcp.
# 3. Lance la commande (supergateway) et surveille /data/.version : quand /setup
#    modifie la configuration, le processus s'arrête et Docker relance le
#    conteneur (restart: unless-stopped), qui relit les nouveaux identifiants.
#    Aucun accès au socket Docker n'est nécessaire.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
GOOGLE_CRED="$DATA_DIR/google/authorized_user.json"
BING_FILE="$DATA_DIR/bing_api_key"
VERSION_FILE="$DATA_DIR/.version"
# google : le service a besoin du jeton Google (ga4)
# google-or-bing : l'un ou l'autre suffit (gsc-bing)
REQUIRES="${REQUIRES:-google}"

log() { echo "[entrypoint] $*" >&2; }

ready() {
  case "$REQUIRES" in
    google) [ -s "$GOOGLE_CRED" ] ;;
    google-or-bing) [ -s "$GOOGLE_CRED" ] || [ -s "$BING_FILE" ] ;;
    *) log "REQUIRES invalide : $REQUIRES"; exit 2 ;;
  esac
}

version() { cat "$VERSION_FILE" 2>/dev/null || echo none; }

stop_requested=0
trap 'stop_requested=1' TERM INT

if ! ready; then
  log "Configuration absente : ouvrez https://<votre-domaine>/setup pour connecter Google et saisir la clé Bing."
  while ! ready; do
    sleep 5 &
    wait $! || true
    [ "$stop_requested" -eq 1 ] && exit 0
  done
  log "Configuration détectée."
fi

if [ -s "$GOOGLE_CRED" ]; then
  export GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_CRED"
else
  unset GOOGLE_APPLICATION_CREDENTIALS 2>/dev/null || true
fi

if [ -s "$BING_FILE" ]; then
  BING_API_KEY="$(cat "$BING_FILE")"
  export BING_API_KEY
fi

started="$(version)"
"$@" &
child=$!

while kill -0 "$child" 2>/dev/null; do
  sleep 5 &
  wait $! || true
  if [ "$stop_requested" -eq 1 ]; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" || true
    exit 0
  fi
  if [ "$(version)" != "$started" ]; then
    log "Configuration modifiée depuis /setup : redémarrage du service."
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" || true
    exit 0
  fi
done

set +e
wait "$child"
code=$?
log "Le serveur s'est arrêté (code $code)."
exit "$code"
