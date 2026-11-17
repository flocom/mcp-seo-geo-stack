"""Page /setup de mcp-seo-geo-stack.

- Saisie du client OAuth Google (type « Application Web ») et de la clé API Bing.
- Connexion Google dans le navigateur, via le tunnel : redirection vers
  https://<domaine>/setup/callback, un seul consentement pour
  webmasters.readonly + analytics.readonly.
- Écrit dans le volume /data un fichier « authorized_user » que lisent à la fois
  search-console-mcp (GOOGLE_APPLICATION_CREDENTIALS) et analytics-mcp
  (google.auth.default), puis incrémente /data/.version pour que les services
  MCP redémarrent et relisent les identifiants.

Bibliothèque standard uniquement : aucune dépendance à maintenir.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# access_jwt.py : copié à côté de app.py dans l'image, dans services/common/ dans le dépôt.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import access_jwt  # noqa: E402

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SETUP_FILE = os.path.join(DATA_DIR, "setup.json")
CRED_FILE = os.path.join(DATA_DIR, "google", "authorized_user.json")
BING_FILE = os.path.join(DATA_DIR, "bing_api_key")
VERSION_FILE = os.path.join(DATA_DIR, ".version")

PORT = int(os.environ.get("PORT", "8080"))
# Refuse toute requête sans jeton Cloudflare Access valide (Cf-Access-Jwt-Assertion,
# signature vérifiée, voir services/common/access_jwt.py). Mettre à 0 uniquement pour un
# test local : l'adresse est alors lue, sans vérification, dans Cf-Access-Authenticated-User-Email.
REQUIRE_ACCESS = os.environ.get("SETUP_REQUIRE_ACCESS", "1") != "0"

SCOPE_GSC = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_GA = "https://www.googleapis.com/auth/analytics.readonly"
SCOPES = ["openid", "email", SCOPE_GSC, SCOPE_GA]

# Points de terminaison externes. Surchargeables par variable d'environnement
# uniquement pour les tests (faux Google : tests/test_setup_flow.py).
_E = os.environ.get
GOOGLE_AUTH_URL = _E("SETUP_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = _E("SETUP_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GOOGLE_REVOKE_URL = _E("SETUP_GOOGLE_REVOKE_URL", "https://oauth2.googleapis.com/revoke")
GOOGLE_USERINFO_URL = _E("SETUP_GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
GSC_SITES_URL = _E("SETUP_GSC_SITES_URL", "https://www.googleapis.com/webmasters/v3/sites")
GA_SUMMARIES_URL = _E("SETUP_GA_SUMMARIES_URL", "https://analyticsadmin.googleapis.com/v1beta/accountSummaries?pageSize=200")
BING_SITES_URL = _E("SETUP_BING_SITES_URL", "https://ssl.bing.com/webmaster/api.svc/json/GetUserSites?apikey=")

HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]+\.apps\.googleusercontent\.com$")
BING_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,128}$")

os.umask(0o077)
_lock = threading.Lock()
# state OAuth -> (expiration, code_verifier, redirect_uri)
_pending: dict[str, tuple[float, str, str]] = {}


# --------------------------------------------------------------------------- #
# Stockage
# --------------------------------------------------------------------------- #
def _write_secret(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def load_setup() -> dict:
    try:
        with open(SETUP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_setup(data: dict) -> None:
    _write_secret(SETUP_FILE, json.dumps(data, indent=2))


def bump_version() -> None:
    """Signale aux services MCP qu'ils doivent redémarrer."""
    _write_secret(VERSION_FILE, f"{time.time_ns()}\n")


def bing_key() -> str:
    try:
        with open(BING_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""


# --------------------------------------------------------------------------- #
# HTTP vers Google / Bing
# --------------------------------------------------------------------------- #
def _http(url: str, data: dict | None = None, token: str | None = None, timeout: int = 15):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    req.add_header("User-Agent", "mcp-seo-geo-stack-setup")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:300]}


def _api_error(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or err)
    return str(payload.get("error_description") or err or payload)[:300]


def check_google(access_token: str) -> list[str]:
    """Vérifie l'accès aux API et renvoie des lignes de diagnostic."""
    lines = []
    status, payload = _http(GSC_SITES_URL, token=access_token)
    if status == 200:
        lines.append(f"Search Console : {len(payload.get('siteEntry', []))} propriété(s) accessible(s).")
    else:
        lines.append(f"Search Console : erreur {status} – {_api_error(payload)}")
    status, payload = _http(GA_SUMMARIES_URL, token=access_token)
    if status == 200:
        props = sum(len(a.get("propertySummaries", [])) for a in payload.get("accountSummaries", []))
        lines.append(f"Google Analytics : {props} propriété(s) GA4 accessible(s).")
    else:
        lines.append(f"Google Analytics : erreur {status} – {_api_error(payload)}")
    return lines


def check_bing(key: str) -> str:
    status, payload = _http(BING_SITES_URL + urllib.parse.quote(key))
    if status == 200:
        return f"Bing Webmaster : {len(payload.get('d') or [])} site(s) accessible(s)."
    return f"Bing Webmaster : erreur {status} – {_api_error(payload)}"


# --------------------------------------------------------------------------- #
# Rendu HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#5e6778;--line:#dde1e8;--accent:#1a5fd6;--ok:#137a3a;--warn:#a15c00;--err:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e7eaf0;--muted:#9aa3b2;--line:#2c3340;--accent:#6ea0ff;--ok:#4cc27a;--warn:#f0a43c;--err:#ff6b5e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:0 0 16px}
label{display:block;font-weight:600;margin:10px 0 4px}input[type=text],input[type=password]{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
button,.btn{display:inline-block;margin-top:12px;padding:9px 16px;border:0;border-radius:7px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
button.secondary{background:transparent;color:var(--err);border:1px solid var(--line)}code{background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:1px 6px;word-break:break-all}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.msg{border-left:4px solid var(--accent);padding:10px 14px;background:var(--card);border-radius:6px;margin:0 0 16px}
.msg.err{border-color:var(--err)}ul{margin:6px 0 0;padding-left:20px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"""
    return doc.encode()


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "mcp-seo-geo-stack-setup"
    sys_version = ""

    # Pas de query string dans les logs (le code OAuth y transite).
    def log_message(self, fmt, *args):
        path = self.path.split("?", 1)[0]
        sys.stderr.write(f"{self.address_string()} {self.command} {path} -> {args[1] if len(args) > 1 else ''}\n")

    # ---- utilitaires ---------------------------------------------------- #
    def _host(self) -> str | None:
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
        return host if HOST_RE.match(host) else None

    def _base_url(self) -> str | None:
        host = self._host()
        if not host:
            return None
        local = host.split(":")[0] in ("localhost", "127.0.0.1")
        return f"{'http' if local else 'https'}://{host}"

    def _access_email(self) -> str:
        """Adresse authentifiée par Cloudflare Access, établie par _guard()."""
        return getattr(self, "access_email", "")

    def _cookies(self) -> cookies.SimpleCookie:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            pass
        return jar

    def _cookie(self, name: str) -> str:
        morsel = self._cookies().get(name)
        return morsel.value if morsel else ""

    def _send(self, status: int, body: bytes, headers: dict | None = None, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            if isinstance(value, list):
                for item in value:
                    self.send_header(key, item)
            else:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str, extra: dict | None = None):
        headers = {"Location": location}
        headers.update(extra or {})
        self._send(303, b"", headers)

    def _flash_redirect(self, msg: str, error: bool = False):
        q = urllib.parse.urlencode({"err" if error else "msg": msg})
        self._redirect(f"/setup?{q}")

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_384:
            return {}
        raw = self.rfile.read(length).decode(errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _guard(self) -> bool:
        """Contrôle d'accès : la requête doit venir de Cloudflare Access."""
        if not REQUIRE_ACCESS:
            self.access_email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").strip()
            return True
        if not access_jwt.configured():
            self._send(503, page("Configuration requise", "<h1>Cloudflare Access n'est pas configuré</h1><p class='sub'>Renseignez <code>ACCESS_TEAM_DOMAIN</code> (ex. <code>mon-equipe.cloudflareaccess.com</code>) et <code>ACCESS_AUD</code> (Application Audience Tag de l'application Access) dans <code>.env</code>, puis <code>docker compose up -d</code>. Tant que ce n'est pas fait, tout accès est refusé.</p>"))
            return False
        try:
            email = access_jwt.email_from((self.headers.get("Cf-Access-Jwt-Assertion") or "").strip())
        except access_jwt.AccessError as exc:
            sys.stderr.write(f"accès refusé : {exc}\n")
            self._send(403, page("Accès refusé", f"<h1>Accès refusé</h1><p class='sub'>Cette page doit être servie derrière Cloudflare Access : {esc(exc)}. Vérifiez l'application Access du domaine.</p>"))
            return False
        self.access_email = email
        owner = load_setup().get("google_account", "")
        if owner and owner.lower() != email.lower():
            self._send(403, page("Accès refusé", f"<h1>Accès refusé</h1><p class='sub'>Cette instance appartient à un autre compte.</p>"))
            return False
        return True

    def _csrf_ok(self, form: dict) -> bool:
        cookie = self._cookie("setup_csrf")
        return bool(cookie) and secrets.compare_digest(cookie, form.get("csrf", ""))

    # ---- routes --------------------------------------------------------- #
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if path == "/setup/healthz":
            return self._send(200, b"ok", ctype="text/plain")
        if path in ("/", ""):
            return self._redirect("/setup")
        if not self._guard():
            return
        if path in ("/setup", "/setup/"):
            return self.render_home(params)
        if path == "/setup/login":
            return self.start_login()
        if path == "/setup/callback":
            return self.finish_login(params)
        self._send(404, page("Introuvable", "<h1>Introuvable</h1>"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._guard():
            return
        form = self._form()
        if not self._csrf_ok(form):
            return self._flash_redirect("Session expirée, réessayez.", error=True)
        routes = {
            "/setup/google-client": self.save_google_client,
            "/setup/bing": self.save_bing,
            "/setup/google-disconnect": self.disconnect_google,
        }
        handler = routes.get(path)
        if not handler:
            return self._send(404, page("Introuvable", "<h1>Introuvable</h1>"))
        handler(form)

    # ---- page d'accueil ------------------------------------------------- #
    def render_home(self, params: dict):
        base = self._base_url()
        if not base:
            return self._send(400, page("Erreur", "<h1>En-tête Host invalide</h1>"))
        cfg = load_setup()
        csrf = self._cookie("setup_csrf") or secrets.token_urlsafe(24)
        set_cookie = f"setup_csrf={csrf}; Path=/setup; HttpOnly; SameSite=Strict" + ("; Secure" if base.startswith("https") else "")
        hidden = f"<input type='hidden' name='csrf' value='{esc(csrf)}'>"
        redirect_uri = f"{base}/setup/callback"
        client_id = cfg.get("client_id", "")
        connected = os.path.exists(CRED_FILE)
        bkey = bing_key()

        parts = [
            "<h1>mcp-seo-geo-stack – configuration</h1>",
            f"<p class='sub'>Connecté via Cloudflare Access en tant que <b>{esc(self._access_email() or 'inconnu (Access désactivé)')}</b>.</p>",
        ]
        if params.get("msg"):
            parts.append(f"<div class='msg'>{esc(params['msg']).replace(chr(10), '<br>')}</div>")
        if params.get("err"):
            parts.append(f"<div class='msg err'>{esc(params['err']).replace(chr(10), '<br>')}</div>")

        # 1. Client OAuth
        status = f"<span class='ok'>configuré</span> (<code>{esc(client_id[:12])}…</code>)" if client_id else "<span class='warn'>à renseigner</span>"
        parts.append(f"""<section class="card"><h2>1. Client OAuth Google – {status}</h2>
<p>Dans Google Cloud Console → <i>APIs &amp; Services → Credentials</i>, créez un « ID client OAuth » de type <b>Application Web</b> avec cet URI de redirection autorisé :</p>
<p><code>{esc(redirect_uri)}</code></p>
<form method="post" action="/setup/google-client">{hidden}
<label for="cid">ID client</label><input id="cid" type="text" name="client_id" value="{esc(client_id)}" placeholder="xxxx.apps.googleusercontent.com" autocomplete="off" required>
<label for="cs">Code secret du client</label><input id="cs" type="password" name="client_secret" placeholder="{'•••••• (laisser vide pour conserver)' if client_id else 'GOCSPX-…'}" autocomplete="off">
<button type="submit">Enregistrer</button></form></section>""")

        # 2. Connexion Google
        if connected:
            scopes = cfg.get("granted_scopes", [])
            missing = [s.rsplit("/", 1)[-1] for s in (SCOPE_GSC, SCOPE_GA) if s not in scopes]
            warn = f"<p class='warn'>Autorisations manquantes : {esc(', '.join(missing))}. Reconnectez-vous en cochant toutes les cases.</p>" if missing else ""
            parts.append(f"""<section class="card"><h2>2. Compte Google – <span class="ok">connecté</span></h2>
<p>Compte : <b>{esc(cfg.get('google_account', '?'))}</b> · depuis le {esc(cfg.get('connected_at', '?'))}</p>{warn}
<div class="row"><a class="btn" href="/setup/login">Reconnecter</a>
<form method="post" action="/setup/google-disconnect">{hidden}<button class="secondary" type="submit">Déconnecter et révoquer</button></form></div></section>""")
        else:
            action = "<a class='btn' href='/setup/login'>Se connecter avec Google</a>" if client_id else "<p class='warn'>Renseignez d'abord le client OAuth (étape 1).</p>"
            parts.append(f"""<section class="card"><h2>2. Compte Google – <span class="warn">non connecté</span></h2>
<p>Un seul consentement pour Search Console (<code>webmasters.readonly</code>) et Analytics (<code>analytics.readonly</code>).</p>{action}</section>""")

        # 3. Bing
        bstatus = f"<span class='ok'>enregistrée</span> (…{esc(bkey[-4:])})" if bkey else "<span class='warn'>non renseignée</span>"
        parts.append(f"""<section class="card"><h2>3. Clé API Bing Webmaster – {bstatus}</h2>
<p>Bing Webmaster Tools → <i>Paramètres → Accès API → Clé API</i>.</p>
<form method="post" action="/setup/bing">{hidden}
<label for="bk">Clé API</label><input id="bk" type="password" name="bing_api_key" autocomplete="off" placeholder="{'•••••• (laisser vide et cocher pour supprimer)' if bkey else ''}">
<label><input type="checkbox" name="delete" value="1"> Supprimer la clé enregistrée</label>
<button type="submit">Enregistrer</button></form></section>""")

        # 4. Connecteurs
        parts.append(f"""<section class="card"><h2>4. Connecteurs Claude</h2>
<p>Réglages Claude → <i>Connecteurs</i> → <i>Ajouter un connecteur personnalisé</i> :</p>
<ul><li>Search Console + Bing : <code>{esc(base)}/gsc-bing/mcp</code></li>
<li>Google Analytics 4 : <code>{esc(base)}/ga4/mcp</code></li></ul>
<p class="sub">Ou une seule URL via un portail MCP Cloudflare (voir le README). Les services redémarrent automatiquement après chaque modification ici.</p></section>""")

        self._send(200, page("mcp-seo-geo-stack – configuration", "".join(parts)), {"Set-Cookie": set_cookie})

    # ---- actions -------------------------------------------------------- #
    def save_google_client(self, form: dict):
        client_id = form.get("client_id", "").strip()
        client_secret = form.get("client_secret", "").strip()
        if not CLIENT_ID_RE.match(client_id):
            return self._flash_redirect("ID client invalide (attendu : …apps.googleusercontent.com).", error=True)
        with _lock:
            cfg = load_setup()
            changed = cfg.get("client_id") != client_id
            if not client_secret:
                if changed or not cfg.get("client_secret"):
                    return self._flash_redirect("Le code secret du client est requis.", error=True)
                client_secret = cfg["client_secret"]
            cfg.update(client_id=client_id, client_secret=client_secret)
            changed = changed and os.path.exists(CRED_FILE)
            if changed:
                # Le jeton existant est lié à l'ancien client : il faut se reconnecter.
                _remove(CRED_FILE)
                cfg.pop("granted_scopes", None)
                bump_version()
            save_setup(cfg)
        self._flash_redirect("Client OAuth enregistré." + (" Reconnectez le compte Google." if changed else ""))

    def start_login(self):
        cfg = load_setup()
        base = self._base_url()
        if not cfg.get("client_id") or not base:
            return self._flash_redirect("Renseignez d'abord le client OAuth.", error=True)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect_uri = f"{base}/setup/callback"
        now = time.time()
        with _lock:
            for key in [k for k, v in _pending.items() if v[0] < now]:
                del _pending[key]
            _pending[state] = (now + 600, verifier, redirect_uri)
        query = urllib.parse.urlencode({
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **({"login_hint": self._access_email()} if self._access_email() else {}),
        })
        secure = "; Secure" if base.startswith("https") else ""
        # SameSite=Lax : le cookie doit revenir lors de la redirection depuis accounts.google.com.
        self._redirect(f"{GOOGLE_AUTH_URL}?{query}", {"Set-Cookie": f"setup_oauth={state}; Path=/setup/callback; Max-Age=600; HttpOnly; SameSite=Lax{secure}"})

    def finish_login(self, params: dict):
        clear = {"Set-Cookie": "setup_oauth=; Path=/setup/callback; Max-Age=0; HttpOnly; SameSite=Lax"}
        state = params.get("state", "")
        with _lock:
            pending = _pending.pop(state, None)
        cookie_state = self._cookie("setup_oauth")
        if not pending or not cookie_state or not secrets.compare_digest(cookie_state, state) or pending[0] < time.time():
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": "État OAuth invalide ou expiré, recommencez."}), clear)
        if params.get("error"):
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": f"Google a refusé : {params['error']}"}), clear)
        _, verifier, redirect_uri = pending
        cfg = load_setup()
        status, tok = _http(GOOGLE_TOKEN_URL, {
            "code": params.get("code", ""),
            "client_id": cfg.get("client_id", ""),
            "client_secret": cfg.get("client_secret", ""),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        })
        if status != 200:
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": f"Échange du code refusé ({status}) : {_api_error(tok)}"}), clear)
        refresh_token = tok.get("refresh_token")
        access_token = tok.get("access_token", "")
        if not refresh_token:
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": "Google n'a pas renvoyé de refresh token. Retirez l'accès de l'application sur myaccount.google.com/permissions puis recommencez."}), clear)

        status, info = _http(GOOGLE_USERINFO_URL, token=access_token)
        email = (info.get("email") or "").strip() if status == 200 else ""
        if not email or not info.get("email_verified", False):
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": "Impossible de lire l'adresse e-mail vérifiée du compte Google."}), clear)
        access_email = self._access_email()
        if access_email and access_email.lower() != email.lower():
            _http(GOOGLE_REVOKE_URL, {"token": refresh_token})
            return self._redirect("/setup?" + urllib.parse.urlencode({"err": f"Compte Google {email} différent du compte autorisé par Cloudflare Access ({access_email}). Jeton révoqué."}), clear)

        granted = (tok.get("scope") or "").split()
        with _lock:
            _write_secret(CRED_FILE, json.dumps({
                "type": "authorized_user",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": refresh_token,
            }, indent=2))
            cfg.update(
                google_account=email,
                granted_scopes=granted,
                connected_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            )
            save_setup(cfg)
            bump_version()
        report = "\n".join([f"Compte Google {email} connecté."] + check_google(access_token))
        self._redirect("/setup?" + urllib.parse.urlencode({"msg": report}), clear)

    def disconnect_google(self, form: dict):
        with _lock:
            try:
                with open(CRED_FILE, encoding="utf-8") as fh:
                    token = json.load(fh).get("refresh_token")
                if token:
                    _http(GOOGLE_REVOKE_URL, {"token": token})
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            _remove(CRED_FILE)
            cfg = load_setup()
            for key in ("granted_scopes", "connected_at"):
                cfg.pop(key, None)
            save_setup(cfg)
            bump_version()
        self._flash_redirect("Compte Google déconnecté et jeton révoqué.")

    def save_bing(self, form: dict):
        if form.get("delete") == "1":
            with _lock:
                _remove(BING_FILE)
                bump_version()
            return self._flash_redirect("Clé Bing supprimée.")
        key = form.get("bing_api_key", "").strip()
        if not BING_KEY_RE.match(key):
            return self._flash_redirect("Clé Bing invalide (caractères alphanumériques attendus).", error=True)
        with _lock:
            _write_secret(BING_FILE, key)
            bump_version()
        self._flash_redirect("Clé Bing enregistrée.\n" + check_bing(key))


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.access(DATA_DIR, os.W_OK):
        sys.exit(f"{DATA_DIR} n'est pas accessible en écriture")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    state = "NON requis (test)" if not REQUIRE_ACCESS else (f"requis, équipe {access_jwt.TEAM_DOMAIN}" if access_jwt.configured() else "NON CONFIGURÉ : tout accès refusé")
    print(f"setup : écoute sur :{PORT} (Cloudflare Access {state})", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
