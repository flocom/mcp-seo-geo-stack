#!/usr/bin/env python3
"""End-to-end test of the /setup app against a mock Google + Bing.

Runs services/setup/app.py as a subprocess (standard library only, Python >= 3.10),
then drives the full flow: Cloudflare Access guard, CSRF, OAuth client storage,
Google login (state + PKCE S256 verified by the mock), callback, account lock,
revocation, Bing key, restart signal (/data/.version) and file permissions.

    python3 tests/test_setup_flow.py
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "services", "setup", "app.py")
OWNER = "owner@example.com"
CLIENT_ID = "123-test.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"
GOOD_BING_KEY = "GOODKEY0123456789ABCDEF"
GSC = "https://www.googleapis.com/auth/webmasters.readonly"
GA = "https://www.googleapis.com/auth/analytics.readonly"


# --------------------------------------------------------------------------- #
# Mock Google / Bing
# --------------------------------------------------------------------------- #
class Mock:
    codes: dict[str, dict] = {}       # code -> {challenge, redirect_uri}
    tokens: dict[str, str] = {}       # access_token -> email
    revoked: list[str] = []
    email = OWNER
    give_refresh = True
    scope = f"openid email {GSC} {GA}"


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    def _bearer_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in Mock.tokens

    def do_POST(self):
        form = self._form()
        if self.path == "/token":
            entry = Mock.codes.pop(form.get("code", ""), None)
            if (form.get("grant_type") != "authorization_code" or form.get("client_id") != CLIENT_ID
                    or form.get("client_secret") != CLIENT_SECRET or not entry
                    or form.get("redirect_uri") != entry["redirect_uri"]):
                return self._json(400, {"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if challenge != entry["challenge"]:
                return self._json(400, {"error": "invalid_grant", "error_description": "PKCE mismatch"})
            access = "ya29." + secrets.token_hex(8)
            Mock.tokens[access] = Mock.email
            resp = {"access_token": access, "expires_in": 3599, "scope": Mock.scope, "token_type": "Bearer"}
            if Mock.give_refresh:
                resp["refresh_token"] = "1//refresh-" + secrets.token_hex(6)
            return self._json(200, resp)
        if self.path == "/revoke":
            Mock.revoked.append(form.get("token", ""))
            return self._json(200, {})
        self._json(404, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/userinfo":
            if not self._bearer_ok():
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, {"email": Mock.tokens[self.headers["Authorization"][7:]], "email_verified": True})
        if path == "/gsc":
            return self._json(200, {"siteEntry": [{"siteUrl": "sc-domain:example.com"}, {"siteUrl": "https://example.org/"}]}) if self._bearer_ok() else self._json(401, {})
        if path == "/ga":
            return self._json(200, {"accountSummaries": [{"propertySummaries": [{}, {}, {}]}]}) if self._bearer_ok() else self._json(401, {})
        if path == "/bing":
            key = urllib.parse.parse_qs(self.path.split("?", 1)[1]).get("apikey", [""])[0]
            return self._json(200, {"d": [{"Url": "https://example.com/"}]}) if key == GOOD_BING_KEY else self._json(400, {"ErrorCode": 3, "Message": "ERROR!!! InvalidApiKey"})
        self._json(404, {})


# --------------------------------------------------------------------------- #
# Client helpers
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port: int, email: str | None = OWNER):
        self.port, self.email, self.jar = port, email, {}

    def req(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if self.email:
            headers["Cf-Access-Authenticated-User-Email"] = self.email
        if self.jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        for raw in resp.headers.get_all("Set-Cookie") or []:
            c = cookies.SimpleCookie(raw)
            for k, m in c.items():
                if m["max-age"] == "0" or m.value == "":
                    self.jar.pop(k, None)
                else:
                    self.jar[k] = m.value
        return resp.status, resp.headers, data

    def flash(self, headers):
        q = urllib.parse.urlparse(headers.get("Location", "")).query
        params = urllib.parse.parse_qs(q)
        return (params.get("msg") or params.get("err") or [""])[0], "err" in params

    def csrf(self):
        if "setup_csrf" not in self.jar:
            self.req("GET", "/setup")
        return self.jar["setup_csrf"]

    def post(self, path, **form):
        form.setdefault("csrf", self.csrf())
        return self.req("POST", path, form)

    def login(self):
        """Simulates the browser round trip through Google."""
        status, headers, _ = self.req("GET", "/setup/login")
        assert status == 303, status
        loc = urllib.parse.urlparse(headers["Location"])
        q = {k: v[0] for k, v in urllib.parse.parse_qs(loc.query).items()}
        code = "4/code-" + secrets.token_hex(6)
        Mock.codes[code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"]}
        return q, self.req("GET", "/setup/callback?" + urllib.parse.urlencode({"state": q["state"], "code": code}))


# --------------------------------------------------------------------------- #
# Test runner
# --------------------------------------------------------------------------- #
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + str(detail)}")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def main() -> int:
    mock_port, app_port = free_port(), free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{mock_port}"
    data = tempfile.mkdtemp(prefix="seo-data-")
    env = dict(os.environ, DATA_DIR=data, PORT=str(app_port),
               SETUP_GOOGLE_TOKEN_URL=f"{base}/token", SETUP_GOOGLE_REVOKE_URL=f"{base}/revoke",
               SETUP_GOOGLE_USERINFO_URL=f"{base}/userinfo", SETUP_GSC_SITES_URL=f"{base}/gsc",
               SETUP_GA_SUMMARIES_URL=f"{base}/ga", SETUP_BING_SITES_URL=f"{base}/bing?apikey=")
    env.pop("SETUP_REQUIRE_ACCESS", None)
    proc = subprocess.Popen([sys.executable, APP], env=env, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", app_port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        cred = os.path.join(data, "google", "authorized_user.json")
        version = os.path.join(data, ".version")
        c = Client(app_port)

        # --- Access guard / basics
        st, _, _ = Client(app_port, email=None).req("GET", "/setup")
        check("refuse request without Cloudflare Access header", st == 403, st)
        st, _, body = Client(app_port, email=None).req("GET", "/setup/healthz")
        check("healthz open without Access (docker healthcheck)", st == 200 and body == "ok", st)
        st, h, body = c.req("GET", "/setup")
        check("home page renders", st == 200 and "/setup/callback" in body and "non connecté" in body, st)
        check("csrf cookie is HttpOnly SameSite=Strict", "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"], h["Set-Cookie"])
        check("security headers present", "frame-ancestors 'none'" in h.get("Content-Security-Policy", "") and h.get("X-Frame-Options") == "DENY")
        st, h, body = c.req("GET", "/setup", None)
        conn = http.client.HTTPConnection("127.0.0.1", app_port)
        conn.request("GET", "/setup", headers={"Host": "evil.com/\"><script>", "Cf-Access-Authenticated-User-Email": OWNER})
        r = conn.getresponse()
        check("invalid Host header rejected", r.status == 400, r.status)

        # --- CSRF
        st, h, _ = c.req("POST", "/setup/google-client", {"csrf": "wrong", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        msg, err = c.flash(h)
        check("POST with bad CSRF token refused", err and not os.path.exists(os.path.join(data, "setup.json")), msg)

        # --- Login before client configured
        st, h, _ = c.req("GET", "/setup/login")
        check("login refused before client configured", "err=" in h.get("Location", ""), h.get("Location"))

        # --- OAuth client
        st, h, _ = c.post("/setup/google-client", client_id="not-a-client", client_secret="x")
        check("invalid client_id rejected", c.flash(h)[1])
        st, h, _ = c.post("/setup/google-client", client_id=CLIENT_ID, client_secret="")
        check("client_secret required on first save", c.flash(h)[1])
        st, h, _ = c.post("/setup/google-client", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
        msg, err = c.flash(h)
        check("client saved (no reconnect hint on first save)", not err and "Reconnectez" not in msg, msg)
        cfg = json.load(open(os.path.join(data, "setup.json")))
        check("setup.json stores client, mode 0600", cfg["client_id"] == CLIENT_ID and mode(os.path.join(data, "setup.json")) == 0o600, oct(mode(os.path.join(data, "setup.json"))))
        st, h, _ = c.post("/setup/google-client", client_id=CLIENT_ID, client_secret="")
        check("empty secret keeps the stored one", not c.flash(h)[1] and json.load(open(os.path.join(data, "setup.json")))["client_secret"] == CLIENT_SECRET)

        # --- Google login: authorization request
        st, h, _ = c.req("GET", "/setup/login")
        loc = urllib.parse.urlparse(h["Location"])
        q = {k: v[0] for k, v in urllib.parse.parse_qs(loc.query).items()}
        check("redirects to Google authorize endpoint", loc.netloc == "accounts.google.com", loc.netloc)
        check("requests offline access + consent", q.get("access_type") == "offline" and q.get("prompt") == "consent", q)
        check("requests exactly the read-only scopes", set(q["scope"].split()) == {"openid", "email", GSC, GA}, q["scope"])
        check("uses PKCE S256", q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) >= 43)
        check("redirect_uri is <host>/setup/callback", q["redirect_uri"] == f"http://127.0.0.1:{app_port}/setup/callback", q["redirect_uri"])
        check("login_hint = Access email", q.get("login_hint") == OWNER, q.get("login_hint"))
        check("oauth state cookie is SameSite=Lax (survives Google redirect)", "SameSite=Lax" in h["Set-Cookie"], h["Set-Cookie"])

        # --- Callback errors
        st, h, _ = c.req("GET", "/setup/callback?state=forged&code=x")
        check("callback with forged state refused", c.flash(h)[1] and not os.path.exists(cred))
        q, (st, h, _) = c.login()
        st2, h2, _ = c.req("GET", "/setup/callback?" + urllib.parse.urlencode({"state": q["state"], "code": "replay"}))
        check("state is single-use", c.flash(h2)[1], c.flash(h2))
        os.remove(cred)  # reset (the first login above succeeded)
        cfg = json.load(open(os.path.join(data, "setup.json")))
        cfg.pop("google_account", None)
        json.dump(cfg, open(os.path.join(data, "setup.json"), "w"))

        Mock.give_refresh = False
        q, (st, h, _) = c.login()
        check("missing refresh_token reported", c.flash(h)[1] and "refresh token" in c.flash(h)[0] and not os.path.exists(cred), c.flash(h))
        Mock.give_refresh = True

        Mock.email = "intruder@example.com"
        before = len(Mock.revoked)
        q, (st, h, _) = c.login()
        check("other Google account refused and token revoked", c.flash(h)[1] and not os.path.exists(cred) and len(Mock.revoked) == before + 1, c.flash(h))
        Mock.email = OWNER

        # --- Successful login
        v0 = open(version).read() if os.path.exists(version) else None
        q, (st, h, _) = c.login()
        msg, err = c.flash(h)
        check("login succeeds", not err and OWNER in msg, msg)
        check("connection report counts GSC + GA4 properties", "2 propriété(s)" in msg and "3 propriété(s) GA4" in msg, msg)
        au = json.load(open(cred))
        check("authorized_user file has the right shape", au["type"] == "authorized_user" and au["client_id"] == CLIENT_ID and au["client_secret"] == CLIENT_SECRET and au["refresh_token"].startswith("1//"), au.keys())
        check("credential file mode 0600, dir 0700", mode(cred) == 0o600 and mode(os.path.dirname(cred)) == 0o700, (oct(mode(cred)), oct(mode(os.path.dirname(cred)))))
        check("restart signal written (.version changed)", os.path.exists(version) and open(version).read() != v0)
        check("oauth cookie cleared after callback", "setup_oauth" not in c.jar, c.jar)
        st, _, body = c.req("GET", "/setup")
        check("home shows connected account", "connecté" in body and OWNER in body and "manquantes" not in body)

        # google-auth must accept the file (same libs as analytics-mcp / search-console-mcp)
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
            creds = Credentials.from_authorized_user_file(cred)
            check("google-auth parses authorized_user file", creds.refresh_token == au["refresh_token"])
        except ImportError:
            print("SKIP  google-auth not installed")

        # --- Account lock: another Access identity is now refused
        st, _, _ = Client(app_port, email="someone@else.com").req("GET", "/setup")
        check("other Access identity refused once owner is set", st == 403, st)
        st, _, _ = Client(app_port, email=OWNER.upper()).req("GET", "/setup")
        check("owner match is case-insensitive", st == 200, st)

        # --- Partial scopes
        Mock.scope = f"openid email {GSC}"
        q, (st, h, _) = c.login()
        st, _, body = c.req("GET", "/setup")
        check("missing analytics scope is flagged", "analytics.readonly" in body and "manquantes" in body)
        Mock.scope = f"openid email {GSC} {GA}"
        c.login()

        # --- Bing
        st, h, _ = c.post("/setup/bing", bing_api_key="bad key!")
        check("malformed Bing key rejected", c.flash(h)[1] and not os.path.exists(os.path.join(data, "bing_api_key")))
        v1 = open(version).read()
        st, h, _ = c.post("/setup/bing", bing_api_key="WRONGKEY0123456789")
        check("invalid Bing key stored but API error reported", "InvalidApiKey" in c.flash(h)[0], c.flash(h))
        st, h, _ = c.post("/setup/bing", bing_api_key=GOOD_BING_KEY)
        bing = os.path.join(data, "bing_api_key")
        check("valid Bing key verified against API", "1 site(s)" in c.flash(h)[0], c.flash(h))
        check("Bing key stored, mode 0600, restart signalled", open(bing).read() == GOOD_BING_KEY and mode(bing) == 0o600 and open(version).read() != v1)
        st, _, body = c.req("GET", "/setup")
        check("page never shows full secrets", GOOD_BING_KEY not in body and CLIENT_SECRET not in body and au["refresh_token"] not in body)
        st, h, _ = c.post("/setup/bing", delete="1")
        check("Bing key deletion", not os.path.exists(bing))

        # --- Client change invalidates the token
        st, h, _ = c.post("/setup/google-client", client_id="999-other.apps.googleusercontent.com", client_secret="GOCSPX-other")
        check("changing OAuth client removes old token and asks to reconnect", "Reconnectez" in c.flash(h)[0] and not os.path.exists(cred), c.flash(h))
        json.dump(dict(json.load(open(os.path.join(data, "setup.json"))), client_id=CLIENT_ID, client_secret=CLIENT_SECRET), open(os.path.join(data, "setup.json"), "w"))
        c.login()

        # --- Disconnect
        refresh = json.load(open(cred))["refresh_token"]
        st, h, _ = c.post("/setup/google-disconnect")
        check("disconnect revokes and deletes token", refresh in Mock.revoked and not os.path.exists(cred), c.flash(h))

        # --- Logs never contain the OAuth code or secrets
        proc.terminate()
        _, stderr = proc.communicate(timeout=5)
        check("logs contain no OAuth code / secrets", "4/code-" not in stderr and CLIENT_SECRET not in stderr and "refresh-" not in stderr, stderr[-500:])
    finally:
        if proc.poll() is None:
            proc.kill()
        mock.shutdown()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
