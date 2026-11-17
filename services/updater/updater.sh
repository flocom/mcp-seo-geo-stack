#!/bin/sh
# Mise à jour automatique de la stack sur le serveur.
#
#   updater watch            (par défaut) Toutes les UPDATE_INTERVAL secondes : git fetch de la
#                            branche suivie, fast-forward, puis redéploiement si le commit a changé.
#   updater deploy <commit>  Lancé par « watch » dans un conteneur à part, où le dépôt est monté à
#                            son chemin réel sur l'hôte : pull (images d'un registre, ex. GHCR) ou
#                            build local des images, puis « docker compose up -d ». Ce conteneur à
#                            part est nécessaire car « up -d » peut recréer le conteneur updater
#                            lui-même.
#
# Chaîne complète : Renovate ouvre une PR → tests CI (lint, OAuth, e2e arm64, builds) → fusion
# automatique si tout est vert → ce service applique la nouvelle version de main sur le serveur.
set -eu

log() { printf '%s updater: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# ---------------------------------------------------------------------------- deploy
registry_images() {
  # Vrai si les images viennent d'un registre (ex. ghcr.io/…) plutôt que d'un build local.
  docker compose config --images setup | grep -Eq '^(localhost|[^/]*[.:][^/]*)/'
}

deploy_stack() {
  target=$1
  if registry_images; then
    # Image construite par GitHub Actions pour ce commit précis (tag sha-xxxxxxx). Si elle n'est
    # pas encore publiée, le pull échoue et « watch » réessaie au cycle suivant. Un IMAGE_TAG
    # fixé dans .env est respecté.
    if ! grep -Eq '^[[:space:]]*IMAGE_TAG=[^[:space:]]' .env 2>/dev/null; then
      IMAGE_TAG="sha-$(printf %s "$target" | cut -c1-7)"
      export IMAGE_TAG
    fi
    log "téléchargement des images (${IMAGE_TAG:-tag de .env})"
    docker compose pull --quiet
  else
    log "construction des images arm64 sur le serveur"
    docker compose pull --quiet --ignore-buildable
    docker compose build --pull --quiet
  fi
  docker compose up -d --remove-orphans
  docker image prune -f >/dev/null
  log "déployé : $target"
}

# ---------------------------------------------------------------------------- watch
self_inspect() { docker inspect -f "$1" "$(hostname)"; }
label() { self_inspect "{{ index .Config.Labels \"$1\" }}"; }
git_repo() { su-exec "$owner" env HOME=/tmp git -C /repo "$@"; }

run_deploy() {
  # Un éventuel déploiement encore en cours (lancé par l'instance précédente de ce conteneur,
  # juste avant d'être recréée) doit se terminer avant d'en lancer un autre.
  if docker container inspect "$helper" >/dev/null 2>&1; then
    docker wait "$helper" >/dev/null 2>&1 || true
    docker rm -f "$helper" >/dev/null 2>&1 || true
  fi
  docker run -d --name "$helper" --network none \
    --security-opt no-new-privileges:true \
    -v "$socket:/var/run/docker.sock" \
    -v "$workdir:$workdir:ro" -w "$workdir" \
    -e COMPOSE_PROJECT_NAME="$project" \
    -e COMPOSE_FILE="$(printf %s "$files" | tr , :)" \
    -e DOCKER_CONFIG=/tmp/.docker \
    --entrypoint /usr/local/bin/updater "$image" deploy "$1" >/dev/null
  code=$(docker wait "$helper")
  docker logs "$helper" 2>&1
  docker rm -f "$helper" >/dev/null 2>&1 || true
  [ "$code" = 0 ]
}

cycle() {
  if ! branch=$(git_repo symbolic-ref --short HEAD); then
    log "le dépôt n'est sur aucune branche : mise à jour impossible"
    return 1
  fi
  if git_repo fetch --quiet origin "$branch"; then
    if ! git_repo merge --quiet --ff-only FETCH_HEAD >/dev/null; then
      log "fast-forward impossible vers origin/$branch (modifications locales ou historique divergent) : à corriger à la main"
    fi
  else
    log "git fetch impossible (réseau ?)"
  fi
  head=$(git_repo rev-parse HEAD)
  [ "$head" != "$deployed" ] || return 0
  [ -z "$deployed" ] || log "nouvelle version : $head"
  if run_deploy "$head"; then
    deployed=$head
  else
    log "échec du déploiement de $head, nouvel essai dans ${interval}s"
    return 1
  fi
}

watch_loop() {
  interval=${UPDATE_INTERVAL:-300}
  case $interval in
    '' | *[!0-9]*) log "UPDATE_INTERVAL invalide : $interval"; exit 1 ;;
  esac
  if [ "$interval" -eq 0 ]; then
    log "mises à jour automatiques désactivées (UPDATE_INTERVAL=0)"
    while :; do sleep 3600; done
  fi

  project=$(label com.docker.compose.project)
  workdir=$(label com.docker.compose.project.working_dir)
  files=$(label com.docker.compose.project.config_files)
  if [ -z "$project" ] || [ -z "$workdir" ] || [ -z "$files" ]; then
    log "ce service doit être lancé par docker compose"
    exit 1
  fi
  image=$(self_inspect '{{.Image}}')
  socket=$(self_inspect '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}')
  # git tourne avec l'uid du propriétaire du dépôt : aucun fichier root dans le clone.
  owner=$(stat -c %u:%g /repo)
  helper="${project}-deploy"
  deployed=

  log "surveille $workdir (projet $project) toutes les ${interval}s"
  while :; do
    cycle || true
    [ -z "${UPDATER_ONCE:-}" ] || break
    sleep "$interval"
  done
}

case "${1:-watch}" in
  watch) watch_loop ;;
  deploy) deploy_stack "${2:?commit attendu}" ;;
  *) echo "usage : updater [watch | deploy <commit>]" >&2; exit 2 ;;
esac
