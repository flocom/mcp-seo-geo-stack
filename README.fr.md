# mcp-seo-geo-stack

🇬🇧 English: [README.md](README.md)

Connecteur Claude **privé** pour **Google Search Console**, **Bing Webmaster Tools** et **Google Analytics 4**. Il tourne entièrement dans Docker sur un serveur **Oracle ARM** (linux/arm64) et il est publié par un **tunnel Cloudflare**. Aucun port n'est ouvert sur le serveur. **Cloudflare Access** en protège l'accès, et seul votre compte Google passe.

- `.env` minimal : `CLOUDFLARE_TUNNEL_TOKEN`, rien d'autre.
- Le client OAuth Google et la clé Bing se saisissent sur la page web `/setup`. Ils sont stockés dans un volume Docker persistant.
- La connexion Google se fait dans le navigateur, sur le serveur publié. Il n'y a aucun fichier JSON d'identifiants à préparer ni à copier.

## Architecture

```
Claude ──HTTPS──▶ Cloudflare Access (OAuth géré + DCR, politique = votre adresse)
                      │
                      ▼  tunnel (sortant uniquement)
                 cloudflared ──┬──▶ setup:8080      /setup, /setup/callback
                               ├──▶ gsc-bing:8000   /gsc-bing/mcp  (search-console-mcp 2.1.4 via supergateway 4.2.2)
                               └──▶ ga4:8000        /ga4/mcp       (analytics-mcp 1.1.0 via supergateway 4.2.2)

volume seo-data (/data) : setup.json (client OAuth, compte), google/authorized_user.json (refresh token), bing_api_key
```

| Service | Rôle |
|---|---|
| `setup` | Page de configuration et flux OAuth web (redirection vers `https://<domaine>/setup/callback`). Écrit dans le volume. Python, bibliothèque standard uniquement. |
| `gsc-bing` | Paquet npm `search-console-mcp`, exposé en Streamable HTTP par `supergateway` (mode stateful). |
| `ga4` | Serveur officiel de Google, paquet PyPI `analytics-mcp`, exposé en Streamable HTTP par `supergateway`. |
| `cloudflared` | Tunnel sortant vers Cloudflare. |

### Choix techniques vérifiés dans le code des paquets

- **search-console-mcp 2.1.4** : son `setup` natif ouvre un serveur sur `127.0.0.1`, ce qui est inutilisable sans écran. Le paquet lit en revanche `GOOGLE_APPLICATION_CREDENTIALS` et passe ce chemin à `google.auth.GoogleAuth({ keyFilename })`. Or google-auth-library accepte un fichier de type **`authorized_user`** (client_id, client_secret, refresh_token). Le paquet lit aussi la clé Bing dans `BING_API_KEY`. Il n'a **pas de dépendance au trousseau** : les appels à `@napi-rs/keyring` sont enveloppés dans des `try/catch`, et le paquet se rabat sur la configuration ou l'environnement. Son fichier de configuration chiffré (`~/.search-console-mcp-config.enc`, clé dérivée du machine-id) n'est pas utilisé ici.
- **analytics-mcp 1.1.0** propose bien un mode `--transport http --oauth`, multi-tenant, avec un serveur d'autorisation OAuth 2.1 qui relaie Google. **Il n'est pas retenu**, pour deux raisons :
  1. il exige son propre jeton Bearer dans `Authorization`, alors que ce même en-tête sert déjà à Cloudflare Access (Managed OAuth). Les deux couches OAuth s'excluent ;
  2. ses clients DCR, ses codes et ses jetons sont gardés **en mémoire** : chaque redémarrage forcerait une nouvelle autorisation, et il faudrait un second consentement Google.
  
  Le service tourne donc en **stdio** avec `google.auth.default()`, qui lit le **même** fichier `authorized_user` que gsc-bing. Vous donnez **un seul consentement** Google pour les deux scopes `webmasters.readonly` et `analytics.readonly`.
- **Redémarrage automatique** : après chaque modification sur `/setup`, l'app incrémente `/data/.version`. Le lanceur commun (`services/common/entrypoint.sh`) voit le changement, arrête le serveur MCP, et Docker relance le conteneur, qui relit les identifiants. Aucun accès au socket Docker n'est nécessaire.
- Les conteneurs tournent en non-root (uid 10001), en lecture seule, sans capabilities. Les services MCP montent le volume en lecture seule.

---

## 1. Projet Google Cloud (une seule fois)

1. Sur [console.cloud.google.com](https://console.cloud.google.com), créez un projet (ou reprenez-en un).
2. **Activez les API** suivantes : *Google Search Console API*, *Google Analytics Admin API*, *Google Analytics Data API*.
3. Configurez l'**écran de consentement OAuth** (*Google Auth Platform*) :
   - type d'utilisateur **Externe** ;
   - ajoutez les scopes `…/auth/webmasters.readonly`, `…/auth/analytics.readonly`, `openid` et `email` ;
   - **Statut de publication : « En production »** (*Publish app*). En mode « Test », le refresh token expire au bout de **7 jours**. En production, il reste valable tant qu'il sert au moins une fois tous les 6 mois et n'est pas révoqué. Comme l'application n'est pas vérifiée par Google, l'écran « Google n'a pas validé cette application » s'affichera : cliquez sur *Paramètres avancés → Accéder à …*. Une vérification n'est pas nécessaire pour votre seul compte.
4. Créez un **ID client OAuth** de type **Application Web** :
   - **URI de redirection autorisé** : `https://seo.example.com/setup/callback`, avec votre domaine (la page `/setup` l'affiche aussi) ;
   - gardez l'ID client et le code secret : vous les saisirez dans `/setup`, jamais dans un fichier.

> Le compte Google qui se connecte doit avoir accès aux propriétés Search Console et GA4 voulues.

## 2. Tunnel Cloudflare

Dans **Cloudflare One / Zero Trust → Networks → Tunnels** :

1. *Create a tunnel* → type **Cloudflared** → nommez-le `mcp-seo-geo-stack`.
2. À l'étape *Install connector*, copiez le **jeton** (la longue chaîne après `--token`). C'est votre `CLOUDFLARE_TUNNEL_TOKEN`.
3. Dans **Public hostnames** (ou *Published application routes*), ajoutez trois routes sur le **même** nom d'hôte, par exemple `seo.example.com`. Le champ *Path* est une expression régulière, et l'ordre des routes compte :

| Hostname | Path | Service |
|---|---|---|
| `seo.example.com` | `^/setup` | `http://setup:8080` |
| `seo.example.com` | `^/gsc-bing/` | `http://gsc-bing:8000` |
| `seo.example.com` | `^/ga4/` | `http://ga4:8000` |

Les noms `setup`, `gsc-bing` et `ga4` sont résolus sur le réseau Docker interne de la stack. Laissez la catch-all par défaut (`http_status:404`).

## 3. Application Cloudflare Access

1. **Settings → Authentication → Login methods** : ajoutez **Google** comme fournisseur d'identité, ou à défaut *One-time PIN*. Ce qui filtre réellement, c'est la politique sur l'adresse e-mail.
2. **Access → Applications → Add an application → Self-hosted** :
   - **Domaine** : `seo.example.com`, sans chemin. L'application couvre ainsi `/setup`, `/gsc-bing/mcp` et `/ga4/mcp` avec **la même politique** ;
   - **Politique** : *Allow*, règle *Include → Emails →* `votre.adresse@gmail.com` ;
   - **Fournisseur d'identité** : Google ; activez *Instant Auth* s'il est le seul ;
   - dans les réglages de l'application, activez **Managed OAuth** (Access sert alors d'authorization server OAuth pour les clients MCP) et **Dynamic Client Registration**. Si l'interface demande une liste de redirections autorisées, ajoutez celle de Claude, `https://claude.ai/api/mcp/auth_callback`.
3. Pour vérifier, ouvrez `https://seo.example.com/setup` en navigation privée : Cloudflare doit vous demander de vous connecter avant d'afficher la page.

> Défense en profondeur : l'app `setup` refuse toute requête sans l'en-tête `Cf-Access-Authenticated-User-Email` ajouté par Access. Elle exige aussi que le compte Google connecté soit **le même** que celui authentifié par Access. Sinon, le jeton est révoqué immédiatement.

## 4. Déploiement sur le serveur Oracle ARM

```bash
git clone https://github.com/flocom/mcp-seo-geo-stack.git
```

```bash
cd mcp-seo-geo-stack && cp .env.example .env && chmod 600 .env
```

Collez le jeton du tunnel dans `.env` (`CLOUDFLARE_TUNNEL_TOKEN=…`), puis :

```bash
docker compose up -d
```

Au premier lancement, les trois images arm64 sont construites nativement sur le serveur. Pour utiliser plutôt les images publiées sur GHCR par GitHub Actions, ajoutez `IMAGE_PREFIX=ghcr.io/flocom/mcp-seo-geo-stack` dans `.env` (et `docker login ghcr.io` si le dépôt est privé), puis :

```bash
docker compose pull && docker compose up -d
```

Tant que `/setup` n'a pas été rempli, `gsc-bing` et `ga4` attendent la configuration : c'est normal. Les journaux le montrent :

```bash
docker compose logs -f gsc-bing ga4
```

## 5. Configuration dans `/setup`

Ouvrez `https://seo.example.com/setup` et authentifiez-vous via Cloudflare Access, puis :

1. **Client OAuth Google** : collez l'ID client et le code secret. L'URI de redirection à déclarer dans Google Cloud est affiché.
2. **Se connecter avec Google** : un seul consentement pour Search Console et Analytics. Cochez toutes les cases. Au retour, la page indique combien de propriétés GSC et GA4 sont accessibles, ou l'erreur exacte (une API non activée, par exemple).
3. **Clé API Bing** : *Bing Webmaster Tools → Paramètres → Accès API → Clé API*. La clé est vérifiée immédiatement auprès de l'API Bing.

Chaque enregistrement relance automatiquement `gsc-bing` et `ga4` en quelques secondes.

## 6. Ajouter le connecteur dans Claude

**Réglages Claude → Connecteurs → Ajouter un connecteur personnalisé** :

| Nom | URL |
|---|---|
| SEO – Search Console + Bing | `https://seo.example.com/gsc-bing/mcp` |
| SEO – Google Analytics 4 | `https://seo.example.com/ga4/mcp` |

Claude découvre l'authentification de Cloudflare Access (Managed OAuth + DCR) et ouvre la fenêtre de connexion Cloudflare. Connectez-vous avec votre compte Google autorisé.

### Option : une seule URL via un portail MCP Cloudflare

Cloudflare One propose des **MCP server portals** (*Access → AI controls / MCP servers*). Un portail regroupe plusieurs serveurs MCP derrière une seule URL et une seule politique Access :

1. déclarez les deux serveurs : `https://seo.example.com/gsc-bing/mcp` et `https://seo.example.com/ga4/mcp` ;
2. créez un portail, par exemple `mcp.example.com`, qui les inclut, avec la même politique (votre adresse e-mail) ;
3. dans Claude, n'ajoutez qu'un seul connecteur : l'URL du portail.

Le nom exact des menus et la façon dont le portail s'authentifie auprès de serveurs eux-mêmes protégés par Access dépendent de l'état actuel de l'offre Cloudflare. Vérifiez dans la documentation « MCP server portals ». Si le portail n'est pas disponible sur votre compte, gardez les deux connecteurs ci-dessus.

---

## Mises à jour

- **Versions fixées** : `services/gsc-bing/package.json` et `package-lock.json` (search-console-mcp, supergateway), `services/ga4/requirements.txt` (analytics-mcp), images de base dans les Dockerfile, `cloudflared` dans `docker-compose.yml`.
- **Renovate** (`renovate.json`) ouvre des PR pour npm, PyPI, les images Docker et les actions GitHub, avec une PR dédiée pour chaque serveur MCP. Activez l'app [Renovate](https://github.com/apps/renovate) sur le dépôt.
- **GitHub Actions** (`.github/workflows/build.yml`) construit les trois images **linux/arm64 nativement** sur un runner `ubuntu-24.04-arm`, sans QEMU ni multi-arch. Les PR (y compris celles de Renovate) sont construites sans être publiées. Sur `main`, les images sont poussées vers `ghcr.io/flocom/mcp-seo-geo-stack/<service>:latest` et `:sha-xxxxxxx`.
- Après fusion d'une PR, sur le serveur :

```bash
git pull && docker compose pull && docker compose up -d
```

  Ou, sans GHCR :

```bash
git pull && docker compose up -d --build
```

## Secrets et sauvegarde

- Les secrets se trouvent **uniquement** dans `.env` (le jeton du tunnel) et dans le volume `mcp-seo-geo-stack_seo-data`. Ces deux emplacements sont exclus de git (`.gitignore`) et du contexte de build (`.dockerignore`), donc jamais copiés dans les images.
- Sauvegarde du volume :

```bash
docker run --rm -v mcp-seo-geo-stack_seo-data:/data:ro -v "$PWD":/out alpine tar czf /out/backup-seo-data.tar.gz -C /data .
```

- **Révocation** : bouton « Déconnecter et révoquer » dans `/setup`, ou [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Dépannage

| Symptôme | Piste |
|---|---|
| `502` sur `/gsc-bing/mcp` ou `/ga4/mcp` | Le service attend la configuration : finissez `/setup`, puis consultez `docker compose logs gsc-bing ga4`. |
| `/setup` répond « Accès refusé… en-tête absent » | La requête n'est pas passée par Access : vérifiez que l'application Access couvre bien le nom d'hôte. |
| `redirect_uri_mismatch` chez Google | L'URI déclaré dans Google Cloud doit être exactement celui affiché dans `/setup`. |
| « pas de refresh token » | Retirez l'application sur myaccount.google.com/permissions, puis reconnectez-vous. |
| Erreur 403 « API has not been used » | Activez l'API indiquée dans le projet Google Cloud du client OAuth. |
| Le jeton expire au bout de 7 jours | L'écran de consentement est resté en mode « Test » : publiez-le en production, puis reconnectez-vous. |
| Outils d'écriture ou d'indexation de search-console-mcp en erreur | Normal : seuls les scopes en lecture seule sont demandés. |

## Arborescence

```
.
├── docker-compose.yml
├── .env.example
├── renovate.json
├── .github/workflows/build.yml
└── services/
    ├── common/entrypoint.sh      # attente de la config + redémarrage sur changement
    ├── setup/{Dockerfile,app.py}
    ├── gsc-bing/{Dockerfile,package.json,package-lock.json}
    └── ga4/{Dockerfile,requirements.txt,package.json,package-lock.json}
```
