#!/usr/bin/env bash
# End-to-end test of the whole Docker stack (linux/arm64), without Cloudflare.
#
#   bash tests/e2e.sh            # builds the images, runs every check, cleans up
#   SKIP_BUILD=1 bash tests/e2e.sh
#
# What it proves:
#   - images are arm64, run as uid 10001, read-only rootfs, all capabilities dropped,
#     no published port in the production compose file;
#   - gsc-bing / ga4 wait for /setup, then start once credentials exist;
#   - /setup and the gate only accept a valid Cloudflare Access JWT (signature checked against a
#     fake Access team): forged header, foreign key, expired token and other users are refused;
#   - gsc-bing / ga4 are only reachable through the gate (not on the tunnel network);
#   - /setup stores the OAuth client and the Bing key;
#   - both MCP servers answer initialize + tools/list over Streamable HTTP, through the gate, and really
#     use the stored credentials (Bing API -> InvalidApiKey, Google -> invalid_client
#     because the test credentials are fake);
#   - a change made in /setup restarts the MCP services automatically;
#   - secrets are 0600 in the volume, read-only for MCP services, absent from images
#     and from the logs.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=mcp-seo-geo-stack-e2e
export CLOUDFLARE_TUNNEL_TOKEN=dummy-token-for-tests
DC=(docker compose -p "$PROJECT" -f docker-compose.yml -f tests/docker-compose.test.yml)
ACCESS_DIR="$(mktemp -d)"
FOREIGN_DIR="$(mktemp -d)"
python3 tests/access_token.py init "$ACCESS_DIR"
export JWKS_DIR="$ACCESS_DIR/srv"
token() { python3 tests/access_token.py mint "$@"; }
JWT=$(token "$ACCESS_DIR" owner@example.com --exp 3600)
ACCESS=(-H "Cf-Access-Jwt-Assertion: $JWT")
GATE=http://127.0.0.1:18003
FAKE_REFRESH="1//fake-refresh-token-e2e"
JAR="$(mktemp)"
passed=0

cleanup() {
  "${DC[@]}" logs --no-color > e2e-logs.txt 2>&1 || true
  "${DC[@]}" down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$JAR" "$ACCESS_DIR" "$FOREIGN_DIR"
}
trap cleanup EXIT

ok()   { passed=$((passed + 1)); echo "PASS  $*"; }
fail() { echo "FAIL  $*" >&2; echo "--- logs" >&2; "${DC[@]}" logs --no-color --tail 80 >&2 || true; exit 1; }
cid()  { "${DC[@]}" ps -q "$1"; }

wait_http() { # url timeout
  local i
  for ((i = 0; i < $2; i++)); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 1; done
  return 1
}

restarts() { docker inspect -f '{{.RestartCount}}' "$(cid "$1")"; }

post() { # path form...
  local path=$1; shift
  local csrf; csrf=$(awk '$6 == "setup_csrf" {print $7}' "$JAR")
  curl -sS -o /dev/null -w '%{redirect_url}' -b "$JAR" -c "$JAR" "${ACCESS[@]}" \
    --data-urlencode "csrf=$csrf" "$@" "http://127.0.0.1:18080$path"
}

urldecode() { python3 -c 'import sys,urllib.parse;print(urllib.parse.unquote_plus(sys.argv[1]))' "$1"; }

echo "== Compose files"
docker compose -f docker-compose.yml config --quiet && ok "docker-compose.yml is valid"
if docker compose -f docker-compose.yml config --format json | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]; sys.exit(any(v.get("ports") for v in s.values()))'; then
  ok "no port published by the production compose file"
else
  fail "a service publishes a port"
fi

echo "== Build"
if [[ -z "${SKIP_BUILD:-}" ]]; then "${DC[@]}" build; fi
for svc in setup gate gsc-bing ga4; do
  arch=$(docker image inspect -f '{{.Architecture}}' "mcp-seo-geo-stack/$svc:latest")
  [[ "$arch" == "arm64" ]] || fail "$svc image is $arch, expected arm64"
done
ok "images are linux/arm64"
docker run --rm --platform linux/arm64 cloudflare/cloudflared:2026.11.0 --version | grep -q "cloudflared version" \
  && ok "cloudflared image runs on arm64"

echo "== Start (unconfigured)"
"${DC[@]}" up -d --wait setup
"${DC[@]}" up -d
sleep 8
for svc in gsc-bing ga4; do
  "${DC[@]}" logs "$svc" | grep -q "Configuration absente" || fail "$svc is not waiting for configuration"
done
ok "gsc-bing and ga4 wait for /setup"
curl -fsS -o /dev/null http://127.0.0.1:18001/gsc-bing/healthz 2>/dev/null && fail "gsc-bing should not serve yet"
ok "MCP endpoints closed before configuration"

echo "== Hardening"
for svc in setup gate gsc-bing ga4; do
  c=$(cid "$svc")
  [[ "$(docker inspect -f '{{.Config.User}}' "$c")" == "10001:10001" ]] || fail "$svc does not run as 10001"
  [[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$c")" == "true" ]] || fail "$svc rootfs is writable"
  docker inspect -f '{{.HostConfig.CapDrop}}' "$c" | grep -q ALL || fail "$svc keeps capabilities"
  docker inspect -f '{{.HostConfig.SecurityOpt}}' "$c" | grep -q no-new-privileges || fail "$svc lacks no-new-privileges"
done
ok "non-root uid 10001, read-only rootfs, cap_drop ALL, no-new-privileges"
"${DC[@]}" exec -T gsc-bing sh -c 'touch /data/x 2>/dev/null' && fail "gsc-bing can write the volume"
"${DC[@]}" exec -T ga4 sh -c 'touch /data/x 2>/dev/null' && fail "ga4 can write the volume"
ok "MCP services mount /data read-only"

echo "== /setup over HTTP"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/setup)
[[ "$code" == "403" ]] || fail "/setup without Access header returned $code"
ok "/setup refuses requests without the Cloudflare Access header"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Cf-Access-Authenticated-User-Email: owner@example.com" http://127.0.0.1:18080/setup)
[[ "$code" == "403" ]] || fail "/setup accepted a forged email header ($code)"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Cf-Access-Jwt-Assertion: $(token "$FOREIGN_DIR" owner@example.com)" http://127.0.0.1:18080/setup)
[[ "$code" == "403" ]] || fail "/setup accepted a JWT signed by another key ($code)"
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Cf-Access-Jwt-Assertion: $(token "$ACCESS_DIR" owner@example.com --exp -120)" http://127.0.0.1:18080/setup)
[[ "$code" == "403" ]] || fail "/setup accepted an expired JWT ($code)"
ok "/setup refuses a forged email header, a foreign-key JWT and an expired JWT"
curl -fsS -c "$JAR" "${ACCESS[@]}" http://127.0.0.1:18080/setup | grep -q "/setup/callback" || fail "home page"
ok "/setup home page renders"
loc=$(post /setup/google-client --data-urlencode "client_id=123-e2e.apps.googleusercontent.com" --data-urlencode "client_secret=GOCSPX-e2e")
[[ "$loc" == *"msg="* ]] || fail "saving OAuth client: $(urldecode "$loc")"
ok "OAuth client saved"
loc=$(post /setup/bing --data-urlencode "bing_api_key=E2EFAKEKEY0123456789")
[[ "$(urldecode "$loc")" == *"InvalidApiKey"* ]] || echo "note: Bing API check said: $(urldecode "$loc")"
ok "Bing key saved (live check: $(urldecode "$loc" | tail -1))"
loc=$(curl -sS -o /dev/null -w '%{redirect_url}' -b "$JAR" -c "$JAR" "${ACCESS[@]}" http://127.0.0.1:18080/setup/login)
[[ "$loc" == https://accounts.google.com/* && "$loc" == *"code_challenge_method=S256"* && "$loc" == *"access_type=offline"* ]] \
  || fail "login redirect: $loc"
ok "Google login redirect (offline, PKCE)"

echo "== Simulated Google callback (real storage functions of app.py)"
"${DC[@]}" exec -T setup python - <<PY
import json, sys
sys.path.insert(0, "/app")
import app
app._write_secret(app.CRED_FILE, json.dumps({
    "type": "authorized_user",
    "client_id": "123-e2e.apps.googleusercontent.com",
    "client_secret": "GOCSPX-e2e",
    "refresh_token": "$FAKE_REFRESH",
}))
cfg = app.load_setup(); cfg.update(google_account="owner@example.com", granted_scopes=app.SCOPES); app.save_setup(cfg)
app.bump_version()
PY
perm=$("${DC[@]}" exec -T setup stat -c '%a %u' /data/google/authorized_user.json /data/bing_api_key /data/setup.json | sort -u)
[[ "$perm" == "600 10001" ]] || fail "secret file permissions: $perm"
ok "secrets stored 0600, owned by 10001"

echo "== MCP servers"
wait_http http://127.0.0.1:18001/gsc-bing/healthz 90 || fail "gsc-bing did not start"
wait_http http://127.0.0.1:18002/ga4/healthz 120 || fail "ga4 did not start"
ok "both MCP services started after configuration"

export MCP_HEADERS="{\"Cf-Access-Jwt-Assertion\":\"$JWT\"}"
TOOL_CALL='{"name":"sites_list","arguments":{"engine":"bing"}}' TOOL_EXPECT='InvalidApiKey|Bing API error' \
  node tests/mcp-smoke.mjs "$GATE/gsc-bing/mcp" 20 sites_list || fail "gsc-bing MCP / Bing key"
ok "gsc-bing (through the gate): MCP handshake, tools/list, Bing API reached with the stored key"
TOOL_CALL='{"name":"sites_list","arguments":{"engine":"google"}}' TOOL_EXPECT='invalid_client|invalid_grant|unauthorized' \
  node tests/mcp-smoke.mjs "$GATE/gsc-bing/mcp" 20 || fail "gsc-bing MCP / Google credentials"
ok "gsc-bing: Google authorized_user credentials loaded"
TOOL_CALL='{"name":"get_account_summaries","arguments":{}}' TOOL_EXPECT='invalid_client|invalid_grant|unauthorized' \
  node tests/mcp-smoke.mjs "$GATE/ga4/mcp" 8 run_report || fail "ga4 MCP"
ok "ga4 (through the gate): MCP handshake, tools/list, same Google credentials loaded"

echo "== Gate: no MCP access without a valid Cloudflare Access JWT"
gate_code() { # path [header]
  curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' ${2:+-H "$2"} \
    --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$GATE$1"
}
for path in /gsc-bing/mcp /ga4/mcp; do
  [[ "$(gate_code "$path")" == "403" ]] || fail "gate let $path through without a token"
  [[ "$(gate_code "$path" "Cf-Access-Authenticated-User-Email: owner@example.com")" == "403" ]] || fail "gate accepted a forged email header on $path"
  [[ "$(gate_code "$path" "Cf-Access-Jwt-Assertion: $(token "$FOREIGN_DIR" owner@example.com)")" == "403" ]] || fail "gate accepted a foreign-key JWT on $path"
  [[ "$(gate_code "$path" "Cf-Access-Jwt-Assertion: $(token "$ACCESS_DIR" owner@example.com --aud other-app)")" == "403" ]] || fail "gate accepted a JWT for another application on $path"
  [[ "$(gate_code "$path" "Cf-Access-Jwt-Assertion: $(token "$ACCESS_DIR" intruder@example.com)")" == "403" ]] || fail "gate accepted another user on $path"
done
ok "gate refuses: no token, forged header, foreign key, wrong audience, user other than the linked Google account"
"${DC[@]}" exec -T setup python -c 'import socket; socket.getaddrinfo("gsc-bing", 8000)' 2> /dev/null \
  && fail "gsc-bing is reachable from the tunnel network"
"${DC[@]}" exec -T setup python -c 'import socket; socket.getaddrinfo("ga4", 8000)' 2> /dev/null \
  && fail "ga4 is reachable from the tunnel network"
"${DC[@]}" exec -T gate python -c 'import socket; socket.getaddrinfo("gsc-bing", 8000); socket.getaddrinfo("ga4", 8000)' \
  || fail "gate cannot reach the MCP servers"
ok "MCP servers reachable only through the gate (not on the tunnel network)"

echo "== Automatic restart after a change in /setup"
before_g=$(restarts gsc-bing); before_a=$(restarts ga4)
loc=$(post /setup/bing --data-urlencode "delete=1")
[[ "$loc" == *"msg="* ]] || fail "delete Bing key: $(urldecode "$loc")"
for _ in $(seq 1 60); do
  [[ "$(restarts gsc-bing)" -gt "$before_g" && "$(restarts ga4)" -gt "$before_a" ]] && break
  sleep 1
done
[[ "$(restarts gsc-bing)" -gt "$before_g" ]] || fail "gsc-bing was not restarted"
[[ "$(restarts ga4)" -gt "$before_a" ]] || fail "ga4 was not restarted"
wait_http http://127.0.0.1:18001/gsc-bing/healthz 90 || fail "gsc-bing did not come back"
wait_http http://127.0.0.1:18002/ga4/healthz 120 || fail "ga4 did not come back"
node tests/mcp-smoke.mjs "$GATE/gsc-bing/mcp" 20 > /dev/null || fail "gsc-bing after restart"
ok "services restarted and serve again after the change"

echo "== No secrets in images or logs"
for svc in setup gate gsc-bing ga4; do
  docker image inspect -f '{{json .Config.Env}}' "mcp-seo-geo-stack/$svc:latest" | grep -Eqi 'token|secret|apikey|api_key' \
    && fail "$svc image env contains a secret-looking variable"
  docker run --rm --entrypoint sh "mcp-seo-geo-stack/$svc:latest" -c 'test -z "$(ls -A /data)"' || fail "$svc image ships data"
done
ok "images contain no credentials"
logs=$("${DC[@]}" logs --no-color setup gate gsc-bing ga4)
grep -qF "$JWT" <<< "$logs" && fail "Access JWT found in logs"
grep -q "$FAKE_REFRESH" <<< "$logs" && fail "refresh token found in logs"
grep -q "E2EFAKEKEY0123456789" <<< "$logs" && fail "Bing key found in logs"
grep -q '"tools":\[' <<< "$logs" && fail "MCP payloads found in logs"
ok "logs contain no token, key or MCP payload"

echo
echo "All $passed e2e checks passed."
