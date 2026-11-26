#!/usr/bin/env python3
"""Passerelle d'authentification devant les serveurs MCP (gsc-bing, ga4).

Le tunnel Cloudflare envoie /gsc-bing/ et /ga4/ ici, jamais directement aux serveurs MCP,
qui ne sont joignables que sur le réseau Docker interne « backend ».

Chaque requête doit porter un jeton Cloudflare Access valide (Cf-Access-Jwt-Assertion :
signature RS256, émetteur, audience, expiration ; voir services/common/access_jwt.py). Si un
compte Google est déjà lié dans /setup, l'adresse du jeton doit être la même. Sinon : 403, et
la requête n'atteint jamais le serveur MCP. Le jeton et les cookies ne sont pas transmis au
serveur MCP. Les réponses (Streamable HTTP, SSE) sont relayées en continu.

Plafond de sessions : les sessions sont servies par quelques processus MCP partagés (services/mux),
elles coûtent peu, mais leur nombre reste borné pour que des clients qui ne ferment jamais leurs
sessions ne s'accumulent pas. La passerelle suit les sessions ouvertes par serveur MCP : au-delà
de GATE_MAX_SESSIONS (100 par défaut), une nouvelle session (initialize) ferme d'abord la plus
ancienne session inactive (DELETE vers le serveur MCP). Le client de cette session reçoit
404 à sa requête suivante et en ouvre une nouvelle, comme le prévoit la spécification MCP. Si
toutes les sessions ont une requête en cours, la nouvelle est refusée (503, Retry-After).

Bibliothèque standard Python uniquement.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# access_jwt.py : copié à côté de gate.py dans l'image, dans services/common/ dans le dépôt.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
import access_jwt  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
SETUP_FILE = os.path.join(os.environ.get("DATA_DIR", "/data"), "setup.json")
MAX_BODY = 10 * 1024 * 1024
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
              "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_FORWARDED = HOP_BY_HOP | {"cf-access-jwt-assertion", "cookie"}


def parse_routes(spec: str) -> list[tuple[str, str, int]]:
    """« /gsc-bing/=gsc-bing:8000,/ga4/=ga4:8000 » -> [(préfixe, hôte, port), ...]"""
    routes = []
    for item in filter(None, (part.strip() for part in spec.split(","))):
        prefix, target = item.split("=", 1)
        host, port = target.rsplit(":", 1)
        routes.append((prefix, host, int(port)))
    return routes


ROUTES = parse_routes(os.environ.get("GATE_ROUTES", "/gsc-bing/=gsc-bing:8000,/ga4/=ga4:8000"))
MAX_SESSIONS = max(1, int(os.environ.get("GATE_MAX_SESSIONS") or "100"))


class Sessions:
    """Sessions MCP ouvertes, par serveur, de la moins récemment utilisée à la plus récente.

    Pour chaque session : nombre de requêtes POST en cours (une session occupée n'est jamais
    fermée pour faire de la place). Le flux GET d'une session ne compte pas : il reste ouvert
    tant que le client est connecté, même inactif.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.lock = threading.Lock()
        self.routes: dict[str, OrderedDict[str, int]] = {}

    def _of(self, route: str) -> OrderedDict[str, int]:
        return self.routes.setdefault(route, OrderedDict())

    def add(self, route: str, sid: str) -> None:
        with self.lock:
            sessions = self._of(route)
            sessions.setdefault(sid, 0)
            sessions.move_to_end(sid)

    def remove(self, route: str, sid: str) -> None:
        with self.lock:
            self._of(route).pop(sid, None)

    def begin(self, route: str, sid: str) -> None:
        with self.lock:
            sessions = self._of(route)
            if sid in sessions:
                sessions[sid] += 1
                sessions.move_to_end(sid)

    def end(self, route: str, sid: str) -> None:
        with self.lock:
            sessions = self._of(route)
            if sid in sessions:
                sessions[sid] = max(0, sessions[sid] - 1)
                sessions.move_to_end(sid)

    def make_room(self, route: str) -> list[str] | None:
        """Sessions à fermer pour en ouvrir une de plus (None : toutes occupées, refuser)."""
        with self.lock:
            sessions = self._of(route)
            victims = []
            while len(sessions) >= self.limit:
                idle = next((sid for sid, busy in sessions.items() if busy == 0), None)
                if idle is None:
                    for sid in victims:  # rien n'est fermé : on remet les sessions choisies
                        sessions[sid] = 0
                    return None
                del sessions[idle]
                victims.append(idle)
            return victims

    def count(self, route: str) -> int:
        with self.lock:
            return len(self._of(route))


SESSIONS = Sessions(MAX_SESSIONS)


def is_initialize(body: bytes) -> bool:
    try:
        message = json.loads(body)
    except ValueError:
        return False
    messages = message if isinstance(message, list) else [message]
    return any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)


def owner() -> str:
    """Compte Google lié dans /setup (vide tant qu'aucune connexion n'a eu lieu)."""
    try:
        with open(SETUP_FILE, encoding="utf-8") as fh:
            return (json.load(fh).get("google_account") or "").strip().lower()
    except (OSError, ValueError, AttributeError):
        return ""


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-seo-geo-stack-gate"
    sys_version = ""

    def log_message(self, fmt, *args):
        path = self.path.split("?", 1)[0]
        sys.stderr.write(f"{self.address_string()} {self.command} {path} -> {args[1] if len(args) > 1 else ''}\n")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        path = self.path.split("?", 1)[0]
        for prefix, host, port in ROUTES:
            if path.startswith(prefix):
                return prefix, host, port
        return None

    def _authorize(self) -> None:
        email = access_jwt.email_from((self.headers.get("Cf-Access-Jwt-Assertion") or "").strip()).lower()
        bound = owner()
        if bound and bound != email:
            raise access_jwt.AccessError("adresse différente du compte lié dans /setup")

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return bytes(body)
                if len(body) + size > MAX_BODY:
                    raise ValueError("body too large")
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _handle(self) -> None:
        if self.path.split("?", 1)[0] == "/gate/healthz":
            return self._json(200, {"status": "ok"})
        target = self._route()
        if not target:
            return self._json(404, {"error": "not_found"})
        try:
            self._authorize()
        except access_jwt.AccessError as exc:
            sys.stderr.write(f"accès refusé : {exc}\n")
            self.close_connection = True
            return self._json(403, {"error": "forbidden", "detail": str(exc)})
        try:
            body = self._read_body()
        except ValueError:
            self.close_connection = True
            return self._json(413, {"error": "payload_too_large"})

        route, host, port = target
        headers = {k: v for k, v in self.headers.items() if k.lower() not in NOT_FORWARDED}
        headers["Host"] = f"{host}:{port}"
        if body or self.command in ("POST", "PUT", "PATCH"):
            headers["Content-Length"] = str(len(body))
        sid = (self.headers.get("Mcp-Session-Id") or "").strip()

        opening = self.command == "POST" and not sid and is_initialize(body)
        if opening:
            victims = SESSIONS.make_room(route)
            if victims is None:
                sys.stderr.write(f"{route} : {MAX_SESSIONS} sessions occupées, nouvelle session refusée\n")
                self.close_connection = True
                self.send_response(503)
                self.send_header("Retry-After", "5")
                body503 = json.dumps({"error": "too_many_sessions"}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body503)))
                self.end_headers()
                self.wfile.write(body503)
                return
            for victim in victims:
                self._close_session(host, port, victim)

        busy = self.command == "POST" and bool(sid)
        if busy:
            SESSIONS.begin(route, sid)
        upstream = http.client.HTTPConnection(host, port, timeout=30)
        try:
            try:
                upstream.request(self.command, self.path, body=body or None, headers=headers)
                upstream.sock.settimeout(None)  # flux SSE : pas de délai d'inactivité
                resp = upstream.getresponse()
            except OSError:
                return self._json(502, {"error": "upstream_unavailable"})
            new_sid = (resp.getheader("Mcp-Session-Id") or "").strip()
            if opening and new_sid and resp.status < 400:
                SESSIONS.add(route, new_sid)
            elif sid and (resp.status == 404 or (self.command == "DELETE" and resp.status < 300)):
                SESSIONS.remove(route, sid)
            self._relay(resp)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # le client est parti
        finally:
            if busy:
                SESSIONS.end(route, sid)
            upstream.close()

    def _close_session(self, host: str, port: int, sid: str) -> None:
        """Ferme une session côté serveur MCP (DELETE, comme le ferait le client)."""
        conn = http.client.HTTPConnection(host, port, timeout=10)
        try:
            conn.request("DELETE", self.path.split("?", 1)[0], headers={"Mcp-Session-Id": sid, "Host": f"{host}:{port}"})
            status = conn.getresponse().status
            sys.stderr.write(f"plafond de {MAX_SESSIONS} sessions atteint : session inactive fermée ({status})\n")
        except OSError as exc:
            sys.stderr.write(f"fermeture d'une session inactive impossible : {exc}\n")
        finally:
            conn.close()

    def _relay(self, resp: http.client.HTTPResponse) -> None:
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        length = resp.getheader("Content-Length")
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        if no_body or length is not None:
            if length is not None:
                self.send_header("Content-Length", length)
            elif not no_body:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if not no_body:
                while chunk := resp.read1(65536):
                    self.wfile.write(chunk)
            return
        # Longueur inconnue (flux SSE) : on retransmet morceau par morceau.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := resp.read1(65536):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _handle


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Gate)
    server.daemon_threads = True
    state = f"équipe {access_jwt.TEAM_DOMAIN}" if access_jwt.configured() else "NON CONFIGURÉ : tout accès refusé"
    routes = ", ".join(f"{p} -> {h}:{port}" for p, h, port in ROUTES)
    print(f"gate : écoute sur :{PORT} (Cloudflare Access {state}) ; {routes} ; "
          f"{MAX_SESSIONS} sessions max par serveur MCP", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
