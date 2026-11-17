#!/usr/bin/env python3
"""Test of services/gate/gate.py against a fake MCP upstream (standard library + openssl).

Checks that no request reaches the MCP server without a valid Cloudflare Access JWT, that the
JWT and cookies are not forwarded, that the account bound in /setup is enforced, and that
responses are relayed correctly: fixed length, chunked SSE (streamed, not buffered) and
close-delimited bodies.

    python3 tests/test_gate.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "services", "gate", "gate.py")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from access_token import DEFAULT_AUD, Key, mint  # noqa: E402

KEY = Key(tempfile.mkdtemp(prefix="access-key-"))
FOREIGN_KEY = Key(tempfile.mkdtemp(prefix="access-foreign-"))
OWNER = "owner@example.com"


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: list[dict] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/certs":
            return self._send(200, json.dumps(KEY.jwks()).encode())
        Upstream.hits.append({"path": self.path, "headers": dict(self.headers)})
        if self.path == "/mcp/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = f"data: event-{i}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                time.sleep(0.6)
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/mcp/close":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"close-delimited body")
            return
        self._send(200, b"get-ok")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        Upstream.hits.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self._send(200, json.dumps({"echo": body.decode()}).encode(), {"Mcp-Session-Id": "session-123"})

    def do_DELETE(self):
        Upstream.hits.append({"path": self.path, "headers": dict(self.headers)})
        self._send(204, b"")

    def _send(self, status, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 204:
            self.wfile.write(body)


results: list[tuple[str, bool]] = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  -> ' + str(detail)}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port):
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)


def main() -> int:
    up_port, gate_port, dead_port = free_port(), free_port(), free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    data = tempfile.mkdtemp(prefix="gate-data-")
    env = dict(os.environ, PORT=str(gate_port), DATA_DIR=data,
               GATE_ROUTES=f"/mcp/=127.0.0.1:{up_port},/dead/=127.0.0.1:{dead_port}",
               ACCESS_TEAM_DOMAIN="e2e.cloudflareaccess.com", ACCESS_AUD=DEFAULT_AUD,
               ACCESS_CERTS_URL=f"http://127.0.0.1:{up_port}/certs")
    proc = subprocess.Popen([sys.executable, GATE], env=env, stderr=subprocess.PIPE, text=True)
    wait_port(gate_port)

    def req(method, path, token=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        h = dict(headers or {})
        if token:
            h["Cf-Access-Jwt-Assertion"] = token
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        return resp, resp.read()

    try:
        good = mint(KEY, OWNER)
        resp, _ = req("GET", "/gate/healthz")
        check("healthz answers without a token", resp.status == 200)
        resp, _ = req("GET", "/elsewhere/", good)
        check("unknown path -> 404", resp.status == 404)

        before = len(Upstream.hits)
        for name, token, extra in [
            ("no token", None, {}),
            ("forged email header", None, {"Cf-Access-Authenticated-User-Email": OWNER}),
            ("token signed by another key", mint(FOREIGN_KEY, OWNER), {}),
            ("expired token", mint(KEY, OWNER, exp=-120), {}),
            ("token for another application", mint(KEY, OWNER, aud="other"), {}),
            ("service token without email", mint(KEY, None), {}),
        ]:
            resp, _ = req("POST", "/mcp/mcp", token, b"{}", {"Content-Type": "application/json", **extra})
            check(f"refuse {name} (403)", resp.status == 403, resp.status)
        check("refused requests never reach the MCP server", len(Upstream.hits) == before, Upstream.hits[before:])

        resp, body = req("POST", "/mcp/mcp", good, b'{"jsonrpc":"2.0"}',
                         {"Content-Type": "application/json", "Cookie": "CF_Authorization=secret", "Mcp-Session-Id": "abc"})
        hit = Upstream.hits[-1]
        lower = {k.lower(): v for k, v in hit["headers"].items()}
        check("valid token: request proxied with its body", resp.status == 200 and json.loads(body)["echo"] == '{"jsonrpc":"2.0"}', body)
        check("Access JWT and cookies not forwarded to the MCP server", "cf-access-jwt-assertion" not in lower and "cookie" not in lower, lower)
        check("MCP headers forwarded both ways", lower.get("mcp-session-id") == "abc" and resp.getheader("Mcp-Session-Id") == "session-123")

        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        conn.putrequest("POST", "/mcp/mcp")
        conn.putheader("Cf-Access-Jwt-Assertion", good)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        resp = conn.getresponse()
        check("chunked request body forwarded", json.loads(resp.read())["echo"] == "hello world")

        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        conn.request("GET", "/mcp/sse", headers={"Cf-Access-Jwt-Assertion": good})
        start = time.time()
        resp = conn.getresponse()
        first = resp.readline()
        first_at = time.time() - start
        rest = resp.read()
        total = time.time() - start
        check("SSE streamed, not buffered (first event before the end)", first.startswith(b"data: event-0") and first_at < 0.5 < total, (first, first_at, total))
        check("SSE stream complete", b"event-2" in rest)

        resp, body = req("GET", "/mcp/close", good)
        check("close-delimited response relayed", resp.status == 200 and body == b"close-delimited body", body)
        resp, body = req("DELETE", "/mcp/mcp", good)
        check("DELETE (end of MCP session) relayed, 204", resp.status == 204 and body == b"")

        resp, _ = req("POST", "/dead/mcp", good, b"{}")
        check("MCP server down -> 502", resp.status == 502)
        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        conn.putrequest("POST", "/mcp/mcp")
        conn.putheader("Cf-Access-Jwt-Assertion", good)
        conn.putheader("Content-Length", str(10 * 1024 * 1024 + 1))
        conn.endheaders()  # the gate answers before reading the body
        check("body over 10 MB -> 413", conn.getresponse().status == 413)

        with open(os.path.join(data, "setup.json"), "w") as fh:
            json.dump({"google_account": "Owner@Example.com"}, fh)
        resp, _ = req("POST", "/mcp/mcp", mint(KEY, "intruder@example.com"), b"{}")
        check("other Access user refused once a Google account is bound", resp.status == 403)
        resp, _ = req("POST", "/mcp/mcp", good, b"{}")
        check("bound owner accepted (case-insensitive)", resp.status == 200)
    finally:
        proc.terminate()
        _, stderr = proc.communicate(timeout=5)
        upstream.shutdown()

    check("logs contain no token", good not in stderr, stderr[-300:])

    bare_port = free_port()
    bare_env = {k: v for k, v in env.items() if not k.startswith("ACCESS_")}
    bare_env["PORT"] = str(bare_port)
    bare = subprocess.Popen([sys.executable, GATE], env=bare_env, stderr=subprocess.DEVNULL)
    try:
        wait_port(bare_port)
        conn = http.client.HTTPConnection("127.0.0.1", bare_port, timeout=10)
        conn.request("POST", "/mcp/mcp", body=b"{}", headers={"Cf-Access-Jwt-Assertion": good})
        check("without ACCESS_TEAM_DOMAIN/ACCESS_AUD everything is refused", conn.getresponse().status == 403)
    finally:
        bare.kill()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
