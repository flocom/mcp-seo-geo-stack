#!/usr/bin/env bash
# End-to-end test of automatic updates (services/updater), without Cloudflare.
#
#   bash tests/updater-e2e.sh
#
# It creates a throwaway "server" checkout whose origin is a local bare repository, starts the
# stack from it (images built locally, as on the Oracle server), then pushes commits the way a
# merged Renovate PR would, and checks that:
#   - the startup deployment is a no-op (no container recreated);
#   - a new commit is fetched, fast-forwarded and deployed within a few seconds: changed services
#     are recreated, including the updater itself, and the data volume is kept;
#   - a broken commit does not take the running stack down, and the next good commit is deployed;
#   - the checkout stays owned by its user (git never runs as root), and no helper container is left.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=$PWD
TMP="$(mktemp -d)"
SERVER="$TMP/server"
DEV="$TMP/dev"
PROJECT=mcp-seo-geo-stack-upd
G=(git -c user.name=e2e -c user.email=e2e@example.com -c init.defaultBranch=main -c advice.detachedHead=false)
passed=0

dc() { (cd "$SERVER" && docker compose "$@"); }
cleanup() {
  dc logs --no-color > "$SRC/updater-e2e-logs.txt" 2>&1 || true
  docker rm -f "$PROJECT-deploy" > /dev/null 2>&1 || true
  dc down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

ok()   { passed=$((passed + 1)); echo "PASS  $*"; }
fail() { echo "FAIL  $*" >&2; echo "--- updater logs" >&2; dc logs --no-color --tail 60 updater >&2 || true; exit 1; }
cid()  { dc ps -q "$1"; }
lbl()  { docker inspect -f '{{index .Config.Labels "e2e.revision"}}' "$(cid "$1")"; }
until_ok() { # timeout command...
  local t=$1 i; shift
  for ((i = 0; i < t; i++)); do "$@" > /dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
deployed()   { dc logs updater | grep -q "déployé : $1"; }
deploy_err() { dc logs updater | grep -q "échec du déploiement de $1"; }
server_at()  { [[ "$(git -C "$SERVER" rev-parse HEAD)" == "$1" ]]; }
labels_are() { [[ "$(lbl setup)" == "$1" && "$(lbl updater)" == "$1" ]]; }
no_helper()  { ! docker container inspect "$PROJECT-deploy"; }
push_dev()   { "${G[@]}" -C "$DEV" commit -qam "$1" && git -C "$DEV" push -q origin main && git -C "$DEV" rev-parse HEAD; }

echo "== Server checkout with a local origin"
mkdir -p "$SERVER"
git archive HEAD | tar -x -C "$SERVER"
"${G[@]}" -C "$SERVER" init -q
"${G[@]}" -C "$SERVER" add -A
"${G[@]}" -C "$SERVER" commit -qm v1
git clone -q --bare "$SERVER" "$SERVER/.git/e2e-origin.git"
# Relative URL: valid both on the host and in the updater container (repository mounted on /repo).
git -C "$SERVER" remote add origin .git/e2e-origin.git
git -C "$SERVER" fetch -q origin
git -C "$SERVER" branch -q -u origin/main
cat > "$SERVER/.env" << EOF
CLOUDFLARE_TUNNEL_TOKEN=dummy-token-for-tests
COMPOSE_PROJECT_NAME=$PROJECT
UPDATE_INTERVAL=3
EOF
"${G[@]}" clone -q "$SERVER/.git/e2e-origin.git" "$DEV"
v1=$(git -C "$SERVER" rev-parse HEAD)
ok "server checkout and developer clone ready"

echo "== Start the stack"
dc up -d --build setup gsc-bing ga4 updater
until_ok 240 deployed "$v1" || fail "startup deployment did not finish"
setup_before=$(cid setup)
updater_before=$(cid updater)
ok "updater running, startup deployment done"
dc exec -T updater sh -c 'docker compose version' | grep -q "Docker Compose" || fail "compose plugin missing in updater image"
ok "updater image ships git, docker and compose"
sleep 5
[[ "$(cid setup)" == "$setup_before" && "$(cid updater)" == "$updater_before" ]] || fail "startup deployment recreated containers"
ok "startup deployment is a no-op"
dc exec -T setup sh -c 'echo keep > /data/e2e-marker'

echo "== A new commit is deployed automatically"
python3 - "$DEV/docker-compose.yml" << 'PY'
import sys
p = sys.argv[1]
s = open(p).read()
for svc in ("setup", "updater"):
    line = "    image: ${IMAGE_PREFIX:-mcp-seo-geo-stack}/%s:${IMAGE_TAG:-latest}\n" % svc
    assert line in s, svc
    s = s.replace(line, line + '    labels: {e2e.revision: "2"}\n', 1)
open(p, "w").write(s)
PY
v2=$(push_dev v2)
until_ok 240 server_at "$v2" || fail "server checkout was not fast-forwarded to v2"
ok "server checkout fast-forwarded to the new commit"
until_ok 240 labels_are 2 || fail "services were not redeployed with v2"
[[ "$(cid setup)" != "$setup_before" ]] || fail "setup was not recreated"
ok "changed services recreated, including the updater itself"
until_ok 120 deployed "$v2" || fail "new updater did not report v2"
dc exec -T setup cat /data/e2e-marker | grep -q keep || fail "data volume lost"
ok "data volume kept across the update"

echo "== A broken commit does not break the running stack"
printf 'this is not: valid: yaml\n' >> "$DEV/docker-compose.yml"
v3=$(push_dev "v3 (broken)")
until_ok 120 server_at "$v3" || fail "server checkout was not fast-forwarded to v3"
until_ok 120 deploy_err "$v3" || fail "failed deployment of v3 not reported"
updater_v2=$(cid updater)
[[ "$(docker inspect -f '{{.State.Running}}' "$(cid setup)")" == true ]] || fail "setup stopped after a broken commit"
sleep 7
[[ "$(cid updater)" == "$updater_v2" && "$(docker inspect -f '{{.State.Running}}' "$updater_v2")" == true ]] \
  || fail "updater stopped after a broken commit"
ok "broken commit reported, stack and updater still running"
"${G[@]}" -C "$DEV" revert --no-edit HEAD > /dev/null
git -C "$DEV" push -q origin main
v4=$(git -C "$DEV" rev-parse HEAD)
until_ok 240 deployed "$v4" || fail "fixed commit v4 was not deployed"
labels_are 2 || fail "labels lost after v4"
ok "next good commit deployed"

echo "== Clean state"
bad=$(find "$SERVER" ! -user "$(id -u)" -print -quit)
[[ -z "$bad" ]] || fail "file not owned by the checkout user: $bad"
ok "checkout files all owned by their user (git never ran as root)"
until_ok 30 no_helper || fail "deploy helper container left behind"
ok "no deploy helper container left"

echo
echo "All $passed updater checks passed."
