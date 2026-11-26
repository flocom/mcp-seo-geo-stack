# mcp-seo-geo-stack — Self-hosted MCP server for Google Search Console, Bing Webmaster Tools & Google Analytics 4 (Claude connector)

**mcp-seo-geo-stack** is a self-hosted, Docker-based [Model Context Protocol (MCP)](https://modelcontextprotocol.io) stack that turns your Google Search Console, Bing Webmaster Tools and Google Analytics 4 data into a **private Claude custom connector**. It runs on an arm64 server (such as Oracle Cloud Ampere), is published through a Cloudflare Tunnel with no open ports, and only your own Google account can use it, enforced by Cloudflare Access.

🇫🇷 Français : [README.fr.md](README.fr.md)

[![build](https://github.com/flocom/mcp-seo-geo-stack/actions/workflows/build.yml/badge.svg)](https://github.com/flocom/mcp-seo-geo-stack/actions/workflows/build.yml)
[![tests](https://github.com/flocom/mcp-seo-geo-stack/actions/workflows/test.yml/badge.svg)](https://github.com/flocom/mcp-seo-geo-stack/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform: linux/arm64](https://img.shields.io/badge/platform-linux%2Farm64-orange.svg)

Ask Claude about your organic search and analytics in plain language. For example: *"Which queries lost clicks in Google and Bing over the last 28 days?"* Claude answers with your live data.

---

## Key features

- **Three data sources, one stack**: Google Search Console API, Bing Webmaster API and the GA4 Data and Admin APIs, all exposed as MCP tools.
- **One Google consent**: a single OAuth sign-in grants `webmasters.readonly` and `analytics.readonly` to both MCP servers.
- **No files to copy**: you sign in to Google in your browser on the published server. No credentials JSON is prepared on another machine.
- **Minimal `.env`**: the tunnel token plus two non-secret Cloudflare Access identifiers (team domain and application AUD). You enter the Google OAuth client and the Bing API key on a web `/setup` page, and they are stored in a persistent Docker volume.
- **Zero open ports**: traffic goes out through a Cloudflare Tunnel. Cloudflare Access (Managed OAuth and Dynamic Client Registration) lets in only your email address.
- **Signed identity, not a header**: every request to `/setup` and to the MCP servers must carry a Cloudflare Access JWT whose RS256 signature, issuer, audience and expiry are verified. Even a route published without Access by mistake exposes nothing.
- **Native arm64**: images are built natively on the Oracle server or on a GitHub ARM runner. No QEMU, no multi-arch.
- **Read-only and hardened**: containers run as non-root, with a read-only filesystem and no Linux capabilities. MCP payloads are not logged.
- **Updates itself**: versions are pinned. Renovate opens a pull request for every npm, PyPI, Docker and Actions update and merges it on its own once the full test suite passes on arm64. The `updater` service then deploys the new version on your server within minutes. No SSH, no manual `git pull`.

## Architecture

```
Claude ──HTTPS──▶ Cloudflare Access (Managed OAuth + DCR, policy = your email)
                      │
                      ▼  tunnel (outbound only)
                 cloudflared ──┬──▶ setup:8080   /setup, /setup/callback        (verifies the Access JWT)
                               └──▶ gate:8080    /gsc-bing/mcp, /ga4/mcp        (verifies the Access JWT)
                                       │  internal "backend" network only
                                       ├──▶ gsc-bing:8000  (search-console-mcp 2.1.4 via mux, 2 shared processes)
                                       └──▶ ga4:8000       (analytics-mcp 1.1.0 via mux, 2 shared processes)

volume seo-data (/data): setup.json (OAuth client, account), google/authorized_user.json (refresh token), bing_api_key
```

| Service | What it does |
|---|---|
| `setup` | Configuration page and web OAuth flow, with Google redirecting to `https://<domain>/setup/callback`. Writes credentials to the volume. Python, standard library only. |
| `gate` | The only way to the MCP servers. Verifies the Cloudflare Access JWT on every request, enforces the Google account linked in `/setup`, then streams the request to `gsc-bing` or `ga4`. Python, standard library only. |
| `gsc-bing` | The npm package [`search-console-mcp`](https://www.npmjs.com/package/search-console-mcp) (Google Search Console + Bing Webmaster Tools), exposed as Streamable HTTP by `mux`, the in-repo multiplexer (`services/mux`). |
| `ga4` | Google's official GA4 MCP server, the PyPI package [`analytics-mcp`](https://pypi.org/project/analytics-mcp/), exposed as Streamable HTTP by `mux`. |
| `cloudflared` | Outbound Cloudflare Tunnel. Nothing listens on the host. |
| `updater` | Automatic updates: checks the GitHub repository every 5 minutes, fast-forwards the server checkout, rebuilds or pulls the images and restarts only what changed. |

## Concurrency and memory

Before, `supergateway` (stateful mode) started one MCP server process per HTTP session (~180 MB each for `search-console-mcp`) and kept it for 30 minutes. A burst of connections could exhaust the server's memory and take `cloudflared` down with it (Cloudflare error `1033`).

Now `gsc-bing` and `ga4` run `mux` (`services/mux/mux.mjs`, Node, whose only dependency is `@modelcontextprotocol/sdk` 1.32.2, pinned in `services/mux/package.json`). It starts `MUX_BACKENDS` (default 2) shared stdio MCP server processes per service and serves any number of Streamable HTTP sessions on top of them. A session costs a few KB: it is an SDK `Server` object that relays `tools/list`, `tools/call`, prompts, resources and completion to the least busy process, with progress notifications, cancellation and a timeout. Sharing is safe because `search-console-mcp` and `analytics-mcp` hold no per-client state: every session uses the same credentials, and the servers never send requests to the client. A process that crashes is restarted on the next request, and open sessions survive. Measured: 50 concurrent sessions (`initialize` + `tools/list` + `tools/call`) all succeeded in about 1 s with ~500 MB RSS in total, versus ~9 GB with one process per session. The stack handles 50+ simultaneous connections.

MCP paths and health endpoints (`/gsc-bing/healthz`, `/ga4/healthz`) are unchanged. Settings go in `.env` (see `.env.example`):

| Variable | Default | Effect |
|---|---|---|
| `GATE_MAX_SESSIONS` | `100` | Sessions per MCP server allowed by `gate`. Beyond it, the least recently used idle session is closed; busy sessions never are. If all are busy, `503` with `Retry-After`. |
| `MUX_BACKENDS` | `2` | Shared MCP server processes per service. |
| `MUX_MAX_SESSIONS` | `200` | Sessions kept open by `mux`; beyond it, the oldest idle one is closed. |
| `MUX_SESSION_IDLE_MS` | `1800000` (30 min) | A session with no request for this long is closed. |
| `MUX_REQUEST_TIMEOUT_MS` | `300000` (5 min) | Maximum duration of a relayed request, reset on each progress notification. |
| `MUX_DEBUG` | off | `1` copies MCP server stderr to the logs. Off by default so MCP payloads (your SEO data) never reach Docker logs. |
| `MCP_MEM_LIMIT` | `1536m` | Memory limit (`mem_limit`) of the `gsc-bing` and `ga4` containers. |

---

## Setup in 6 steps

### 1. Google Cloud project (one time)

1. In [Google Cloud Console](https://console.cloud.google.com), create or pick a project.
2. **Enable these APIs**: *Google Search Console API*, *Google Analytics Admin API* and *Google Analytics Data API*.
3. Configure the **OAuth consent screen** (Google Auth Platform):
   - user type: **External**;
   - scopes: `…/auth/webmasters.readonly`, `…/auth/analytics.readonly`, `openid` and `email`;
   - **publishing status: "In production"**. In "Testing" mode, refresh tokens expire after **7 days**. The app is unverified, so Google shows a warning: click *Advanced → Go to …*. You don't need verification for your own account.
4. Create an **OAuth client ID** of type **Web application**:
   - **Authorized redirect URI**: `https://seo.example.com/setup/callback`, using your own domain. The `/setup` page shows the exact value.
   - Keep the client ID and secret. You will paste them into `/setup`, never into a file.

> The Google account you sign in with must have access to the Search Console and GA4 properties you want.

### 2. Cloudflare Tunnel

In **Cloudflare One / Zero Trust → Networks → Tunnels**:

1. *Create a tunnel* → **Cloudflared** → name it `mcp-seo-geo-stack`.
2. In *Install connector*, copy the **token** (the long string after `--token`). This is your `CLOUDFLARE_TUNNEL_TOKEN`.
3. Under **Public hostnames** (or *Published application routes*), add three routes on the **same** hostname. *Path* is a regular expression, and the order of the routes matters:

| Hostname | Path | Service |
|---|---|---|
| `seo.example.com` | `^/setup` | `http://setup:8080` |
| `seo.example.com` | `^/gsc-bing/` | `http://gate:8080` |
| `seo.example.com` | `^/ga4/` | `http://gate:8080` |

Optional, recommended: in each route's settings, under *Access*, enable **Protect with Access** (`originRequest.access.required`) with your team name and the application AUD. `cloudflared` then rejects requests without a valid Access token before they even reach the containers.

### 3. Cloudflare Access (Managed OAuth + DCR)

1. **Settings → Authentication → Login methods**: add **Google** as the identity provider, or *One-time PIN* as a fallback.
2. **Access → Applications → Add an application → Self-hosted**:
   - **Domain**: `seo.example.com`, with no path, so one policy covers `/setup`, `/gsc-bing/mcp` and `/ga4/mcp`;
   - **Policy**: *Allow → Include → Emails →* `you@example.com`;
   - in the application settings, enable **Managed OAuth** (Access acts as the OAuth authorization server for MCP clients) and **Dynamic Client Registration**. If the UI asks for allowed redirect URIs, add Claude's: `https://claude.ai/api/mcp/auth_callback`.
3. Note two values for `.env` (they are identifiers, not secrets):
   - `ACCESS_TEAM_DOMAIN`: your team domain, for example `my-team.cloudflareaccess.com` (*Settings → Custom pages*, or the team name chosen at sign-up);
   - `ACCESS_AUD`: the **Application Audience (AUD) Tag**, on the application's *Overview* tab.
4. To check, open `https://seo.example.com/setup` in a private window. Cloudflare must ask you to log in first.

> Defense in depth: `setup` and `gate` do not trust any plain header. They verify the `Cf-Access-Jwt-Assertion` JWT that Access adds to every request: RS256 signature against your team's public keys, issuer, audience (AUD) and expiry. Without `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`, they refuse everything. `setup` also requires the Google account you connect to be **the same** as the one Access authenticated, and revokes the token otherwise. Once that account is linked, `gate` refuses every other Access user.

### 4. Deploy on the arm64 server

```bash
git clone https://github.com/flocom/mcp-seo-geo-stack.git
```

```bash
cd mcp-seo-geo-stack && cp .env.example .env && chmod 600 .env
```

Fill in `.env`: the tunnel token (`CLOUDFLARE_TUNNEL_TOKEN=…`), `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD`. Then:

```bash
docker compose up -d
```

On first run, the arm64 images are built natively on the server. To use the images published on GHCR instead, set `IMAGE_PREFIX=ghcr.io/flocom/mcp-seo-geo-stack` in `.env`, then:

```bash
docker compose pull && docker compose up -d
```

Until `/setup` is completed, `gsc-bing` and `ga4` wait for configuration. This is expected.

From now on, the server keeps itself up to date: see [Automatic updates](#automatic-updates). Keep the clone on the `main` branch and do not edit tracked files on the server (settings go in `.env`, which git ignores).

### 5. Configure on the `/setup` page

Open `https://seo.example.com/setup`:

1. **Google OAuth client**: paste the client ID and secret. The page shows the redirect URI to declare in Google Cloud.
2. **Sign in with Google**: one consent covers Search Console and Analytics. Tick every box. When you come back, the page shows how many GSC and GA4 properties are reachable, or the exact error.
3. **Bing API key**: *Bing Webmaster Tools → Settings → API access → API Key*. The key is checked immediately against the Bing API.

Each save restarts `gsc-bing` and `ga4` within a few seconds.

### 6. Add the custom connector in Claude

**Claude settings → Connectors → Add custom connector**:

| Name | URL |
|---|---|
| SEO – Search Console + Bing | `https://seo.example.com/gsc-bing/mcp` |
| SEO – Google Analytics 4 | `https://seo.example.com/ga4/mcp` |

Claude discovers Cloudflare Access authentication through Managed OAuth and DCR, and opens the Cloudflare login. Sign in with your authorized Google account.

#### Optional: a single URL with a Cloudflare MCP server portal

Cloudflare One offers **MCP server portals**, which group several MCP servers behind one URL and one Access policy:

1. declare both servers;
2. create a portal (for example `mcp.example.com`) with the same email policy;
3. add only the portal URL in Claude.

The exact menu names, and how a portal authenticates to servers that are themselves protected by Access, depend on Cloudflare's current offering. Check the "MCP server portals" documentation. If portals are not available on your account, keep the two connectors above.

---

## Example prompts

- "Which queries lost clicks in Google and Bing over the last 28 days?"
- "Compare GA4 sessions vs Search Console clicks for /blog over the last quarter."
- "Find striking-distance keywords (positions 8–20) with high impressions on example.com."
- "Are any of my pages cannibalizing each other in Google Search?"
- "Which queries look like AI / conversational searches? (GEO insights)"
- "Inspect the indexing status of https://example.com/pricing on Google and Bing."
- "Run a GA4 report of key events by landing page for the last 30 days."

## MCP tools exposed

**gsc-bing** (`/gsc-bing/mcp`, search-console-mcp): `get_started`, `sites_list`, `sitemaps_list`, `sitemaps_submit`, `sitemaps_delete`, `analytics_query`, `analytics_compare`, `analytics_anomalies`, `genai_query_insights`, `inspection_inspect`, `pagespeed_analyze`, `seo_audit`, `seo_keywords_research`, `schema_validate`, `site_health_check`, `compare_engines`, `diagnostics`, `indexing_submit`, `indexing_status`, and more.

**ga4** (`/ga4/mcp`, analytics-mcp): `get_account_summaries`, `get_property_details`, `list_google_ads_links`, `list_property_annotations`, `list_key_events`, `get_custom_dimensions_and_metrics`, `run_report`, `run_realtime_report`, `run_funnel_report`, `run_conversions_report`.

Only read-only scopes are requested. Write and indexing tools return an authorization error, which is expected.

## Security model

- **Network**: no published ports. `cloudflared` only reaches `setup` and `gate`. The MCP servers sit on a separate internal network that only `gate` can reach.
- **Identity**: Cloudflare Access protects every path, with a single-email policy. `setup` and `gate` independently verify the signed Access JWT (signature, issuer, AUD, expiry) on every request, so a forged header, a token from another team or application, or an expired token is refused, even if a route were published without Access. Once a Google account is linked, only that email gets through `gate`.
- **Secrets**: they live only in `.env` (tunnel token; the Access team domain and AUD are not secret) and in the `mcp-seo-geo-stack_seo-data` volume, with files at mode `0600`. Secrets are excluded from git (`.gitignore`) and from the build context (`.dockerignore`), so they never end up in an image.
- **Containers**: non-root (uid 10001), `read_only`, `cap_drop: ALL`, `no-new-privileges`. MCP services mount the volume read-only.
- **Logs**: `mux` never logs MCP payloads (your SEO data), and it drops the MCP servers' stderr unless `MUX_DEBUG=1`, so none of it reaches Docker logs.
- **OAuth**: PKCE, a random `state`, a CSRF token on every form, and revocation from `/setup`.
- **Updater**: it mounts the Docker socket, which is equivalent to root on the host, so that it can redeploy the stack. It runs on its own network with no access to the MCP services, without capabilities other than `SETUID`/`SETGID` (to run git as the owner of the checkout), and it only deploys what is merged into the repository's `main` branch. Anyone who can push to that branch can change what runs on your server: protect it accordingly, or set `UPDATE_INTERVAL=0` to turn automatic updates off.

## Automatic updates

Every update follows the same path, with no manual step:

```
Renovate PR ──▶ CI on arm64 (lint, OAuth flow, Docker e2e, image builds) ──▶ auto-merge into main
                                                                                 │
                         server: updater ──▶ git fetch + fast-forward ◀──────────┘ (every 5 minutes)
                                          ──▶ build (or pull from GHCR) ──▶ docker compose up -d
```

- **Pinned versions** live in `services/gsc-bing/package.json` and `package-lock.json`, `services/mux/package.json` and `package-lock.json`, `services/ga4/requirements.txt`, the Dockerfiles, and the `cloudflared` image tag. Base images are also pinned by digest, so even an upstream security rebuild arrives as a tested pull request.
- **Renovate** (`renovate.json`) opens pull requests for npm, PyPI, Docker and GitHub Actions, with a dedicated PR for each MCP server, and **merges them automatically once every check is green**. A red check blocks the merge, and the PR waits for you. New npm and PyPI releases must be 3 days old before they are proposed, a common guard against compromised packages. Install the [Renovate GitHub app](https://github.com/apps/renovate) on your repository (or fork) to turn this on.
- **GitHub Actions** (`build.yml`) builds the four images **natively on `ubuntu-24.04-arm`** and pushes them to `ghcr.io/flocom/mcp-seo-geo-stack/<service>` with the tags `latest` and `sha-<commit>`. Pull requests are built but not published.
- **The `updater` service** on the server checks the repository every 5 minutes (`UPDATE_INTERVAL` in `.env`, in seconds). When `main` has moved, it fast-forwards the checkout, then:
  - by default, rebuilds the images natively on the server;
  - with `IMAGE_PREFIX=ghcr.io/flocom/mcp-seo-geo-stack` in `.env`, pulls the images built by GitHub Actions for that exact commit (`sha-<commit>`), and waits until they are published;
  - runs `docker compose up -d`, which restarts only the services that changed, including the updater itself, and removes unused images.

  The data volume is kept. A commit that fails to deploy leaves the running stack untouched and is retried at the next check. Follow it with:

```bash
docker compose logs -f updater
```

To turn automatic updates off, set `UPDATE_INTERVAL=0` in `.env`, then run `docker compose up -d`. To update by hand instead:

```bash
git pull && docker compose up -d --build
```

## Testing

| Test | What it checks | Command |
|---|---|---|
| `tests/test_setup_flow.py` | The full `/setup` OAuth flow against a mock Google: Access JWT verification (forged header, foreign key, expired, wrong audience or issuer, `alg: none`, tampered payload), CSRF, PKCE, state, account matching, file permissions, revocation | `python3 tests/test_setup_flow.py` |
| `tests/test_gate.py` | The gate against a fake MCP server: no request gets through without a valid JWT, JWT and cookies not forwarded, linked-account lock, SSE streaming | `python3 tests/test_gate.py` |
| `tests/test_gate_sessions.py` | Session cap: least recently used idle session closed first, busy sessions never closed, `503` when all are busy | `python3 tests/test_gate_sessions.py` |
| `tests/test_mux.mjs` | The multiplexer: 50 concurrent sessions served by 2 shared processes, progress relayed, cancellation, crash and restart, `404` for an unknown session, session cap | `cd services/mux && npm ci && node ../../tests/test_mux.mjs` |
| `tests/mcp-smoke.mjs` | MCP over Streamable HTTP: `initialize`, `tools/list`, and an optional `tools/call` | `node tests/mcp-smoke.mjs <url>` |
| `tests/e2e.sh` | The full Docker stack on an arm64 runner: images, wait-for-config, credential reload, both MCP endpoints | `bash tests/e2e.sh` |
| `tests/updater-e2e.sh` | Automatic updates against a local git origin: new commit deployed (updater included), data kept, broken commit harmless, no root-owned files | `bash tests/updater-e2e.sh` |

The `test.yml` workflow runs all of them on every push and pull request.

## Backup

```bash
docker run --rm -v mcp-seo-geo-stack_seo-data:/data:ro -v "$PWD":/out alpine tar czf /out/backup-seo-data.tar.gz -C /data .
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `502` on `/gsc-bing/mcp` or `/ga4/mcp` | The service is waiting for configuration. Complete `/setup`, then check `docker compose logs gsc-bing ga4`. |
| `/setup` says "Accès refusé … jeton Cloudflare Access absent" | The request bypassed Access. Make sure the Access application covers the hostname. |
| `/setup` says "Cloudflare Access n'est pas configuré" (503), MCP calls get `403` | Set `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` in `.env`, then `docker compose up -d`. |
| `403` "jeton destiné à une autre application" | `ACCESS_AUD` does not match the AUD tag of the Access application that protects the hostname. |
| `redirect_uri_mismatch` from Google | The URI in Google Cloud must exactly match the one shown in `/setup`. |
| "No refresh token" | Remove the app at myaccount.google.com/permissions, then reconnect. |
| `403 API has not been used` | Enable that API in the OAuth client's Google Cloud project. |
| Token expires after 7 days | The consent screen is still in "Testing". Publish it to production and reconnect. |
| Cloudflare error `1033` (tunnel unreachable), connectors drop when several requests run at once | The server ran out of memory and took `cloudflared` down. This came from `supergateway`, which started one MCP server process (~180 MB) per session. It is replaced by `mux`, which serves every session from 2 shared processes per service (see [Concurrency and memory](#concurrency-and-memory)): 50 sessions use ~500 MB in total. `gate` allows `GATE_MAX_SESSIONS` (default 100) sessions per MCP server and each MCP container has a memory limit (`MCP_MEM_LIMIT`, default `1536m`). Make sure the server runs the current version, then check with `docker compose ps`, `docker stats --no-stream` and `sudo dmesg -T \| grep -i oom`. On a very small instance, lower `GATE_MAX_SESSIONS` or `MUX_BACKENDS`. |
| Updates are no longer applied | Check `docker compose logs updater`. "fast-forward impossible" means tracked files were edited on the server or the branch diverged: run `git status`, then `git stash` or `git reset --hard origin/main`. |

## FAQ

### How do I connect Google Search Console to Claude?
Deploy this stack, sign in to Google on the `/setup` page, then add `https://<your-domain>/gsc-bing/mcp` as a custom connector in Claude's settings. Claude can then query your Search Console performance, sitemaps, URL inspection and SEO audits.

### Can I use Bing Webmaster Tools with an MCP server?
Yes. The `gsc-bing` service wraps `search-console-mcp`, which supports the Bing Webmaster API. Paste your Bing API key in `/setup`, and the tools will cover Google and Bing side by side, for example `compare_engines`.

### How do I connect Google Analytics 4 (GA4) to Claude?
Add `https://<your-domain>/ga4/mcp` as a second connector. It runs Google's official `analytics-mcp` server with the same Google sign-in, so there is no second consent.

### Does it work on Oracle Cloud Free Tier ARM (Ampere A1)?
Yes. It was designed for it. Everything is `linux/arm64` and built natively. The stack is light: two Node processes, one Python process and `cloudflared`.

### Is my data sent to a third party?
Your data flows directly between your server and the Google and Bing APIs, and between Claude and your server through Cloudflare. Credentials never leave your Docker volume. Nothing is logged by the MCP gateways.

### Why not use analytics-mcp's built-in OAuth mode?
`analytics-mcp` 1.1.0 has a multi-tenant HTTP mode with its own OAuth server. However, it needs its own Bearer token in the `Authorization` header, which conflicts with Cloudflare Access Managed OAuth, and it keeps clients and tokens in memory, so every restart would force a new authorization. Running it over stdio with a shared `authorized_user` credential gives one consent and survives restarts.

### Will the Google refresh token expire?
Not if the OAuth consent screen is **In production**. It stays valid until you revoke it or leave it unused for 6 months. In "Testing" mode it expires after 7 days.

### How does the server stay up to date?
Renovate opens a pull request for each new version of the MCP servers, the base images, `cloudflared` and the GitHub Actions. It merges the pull request automatically once the tests pass on arm64. The `updater` service on your server notices the new commit within 5 minutes and redeploys only what changed. You do nothing.

### Does it help with GEO (Generative Engine Optimization)?
The `genai_query_insights` tool flags queries that look like AI-assisted or conversational searches in Google and Bing data. Combined with GA4 reports, it helps you track how your site performs in AI-driven search.

## Technical notes (verified in the packages' source code)

- **search-console-mcp 2.1.4**: its built-in `setup` starts a server on `127.0.0.1`, which is unusable on a headless server. It does read `GOOGLE_APPLICATION_CREDENTIALS` and passes it to `google.auth.GoogleAuth({ keyFilename })`, and google-auth-library accepts an **`authorized_user`** file (client_id, client_secret, refresh_token). It reads `BING_API_KEY` for Bing. Calls to `@napi-rs/keyring` sit inside `try/catch`, so there is **no hard dependency on an OS keychain**. Its encrypted config file (`~/.search-console-mcp-config.enc`) is not used here.
- **analytics-mcp 1.1.0**: in stdio mode it uses `google.auth.default()`, which loads the **same** `authorized_user` file.
- **Automatic reload**: after each change, `/setup` bumps `/data/.version`. The shared launcher (`services/common/entrypoint.sh`) sees it, stops the MCP server, and Docker restarts the container (`restart: unless-stopped`). No Docker socket is needed for this.
- **mux** (`services/mux/mux.mjs`) replaces supergateway 4.2.2, whose stateful mode started one MCP child process per session (~180 MB each, kept 30 minutes) and exhausted the server's memory under load. mux starts 2 stdio MCP server processes per service (`MUX_BACKENDS`) and gives each Streamable HTTP session its own MCP SDK `Server` object, which relays requests to the least busy process with progress, cancellation and a timeout. This works because search-console-mcp and analytics-mcp are stateless per client: same credentials for everyone and no server-to-client requests (sampling, roots, elicitation). A crashed process is restarted on the next request without closing sessions.

## Project layout

```
.
├── docker-compose.yml
├── .env.example
├── renovate.json
├── .github/workflows/{build.yml,test.yml}
├── tests/{test_setup_flow.py,test_gate.py,test_gate_sessions.py,test_mux.mjs,fake-mcp-server.mjs,mcp-smoke.mjs,mcp-load.mjs,e2e.sh,updater-e2e.sh}
└── services/
    ├── common/entrypoint.sh      # wait for config + restart on change
    ├── common/access_jwt.py      # Cloudflare Access JWT verification
    ├── setup/{Dockerfile,app.py}
    ├── gate/{Dockerfile,gate.py} # auth gateway in front of the MCP servers
    ├── mux/{package.json,mux.mjs} # multiplexer: many MCP sessions, 2 shared MCP server processes
    ├── gsc-bing/{Dockerfile,package.json,package-lock.json}
    ├── ga4/{Dockerfile,requirements.txt}
    └── updater/{Dockerfile,updater.sh}   # automatic updates
```

## License

[MIT](LICENSE) © 2026 flocom
