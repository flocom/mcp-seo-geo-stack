# Mise à jour automatique : client Docker (avec les plugins compose et buildx) + git.
# Aucun secret dans l'image : le dépôt et le socket Docker sont montés à l'exécution.
FROM docker:29.10.0-cli

RUN apk add --no-cache git su-exec

COPY --chmod=0755 services/updater/updater.sh /usr/local/bin/updater

ENTRYPOINT ["/usr/local/bin/updater"]
CMD ["watch"]
