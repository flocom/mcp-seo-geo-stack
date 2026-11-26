#!/usr/bin/env python3
"""Test of the MCP session cap in services/gate/gate.py (standard library + openssl).

The gate must keep at most
GATE_MAX_SESSIONS sessions open per MCP server: a new session closes the least recently used
idle one first (DELETE upstream), never a session with a request in flight, and is refused
with 503 + Retry-After when every session is busy.

    python3 tests/test_gate_sessions.py
"""

from __future__ import annotations

import http.client
import itertools
import json
import os
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
from test_gate import check, free_port, results, wait_port  # noqa: E402

KEY = Key(tempfile.mkdtemp(prefix="access-key-"))
INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
CALL = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "slow"}}).encode()


class FakeGateway(BaseHTTPRequestHandler):
    """Like an MCP Streamable HTTP server: initialize opens a session, DELETE closes it, unknown id -> 404."""

    protocol_version = "HTTP/1.1"
    ids = itertools.count(1)
    open: set[str] = set()
    deleted: list[str] = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/certs":
            return self._send(200, json.dumps(KEY.jwks()).encode())
        self._send(404, b"{}")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        sid = self.headers.get("Mcp-Session-Id")
        if not sid:
            with self.lock:
                new = f"s{next(self.ids)}"
                self.open.add(new)
            return self._send(200, b'{"result":{}}', {"Mcp-Session-Id": new})
        if sid not in self.open:
            return self._send(404, b'{"error":"Session not found"}')
        if b'"slow"' in body:
            time.sleep(1.5)
        self._send(200, b'{"result":"ok"}')

    def do_DELETE(self):
        sid = self.headers.get("Mcp-Session-Id")
        with self.lock:
            known = sid in self.open
            self.open.discard(sid)
            if known:
                self.deleted.append(sid)
        self._send(200 if known else 404, b"")

    def _send(self, status, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    up_port, gate_port = free_port(), free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), FakeGateway)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    env = dict(os.environ, PORT=str(gate_port), DATA_DIR=tempfile.mkdtemp(prefix="gate-data-"),
               GATE_ROUTES=f"/mcp/=127.0.0.1:{up_port}", GATE_MAX_SESSIONS="2",
               ACCESS_TEAM_DOMAIN="e2e.cloudflareaccess.com", ACCESS_AUD=DEFAULT_AUD,
               ACCESS_CERTS_URL=f"http://127.0.0.1:{up_port}/certs")
    proc = subprocess.Popen([sys.executable, GATE], env=env, stderr=subprocess.PIPE, text=True)
    wait_port(gate_port)
    token = mint(KEY, "owner@example.com")

    def post(body, sid=None):
        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        headers = {"Cf-Access-Jwt-Assertion": token, "Content-Type": "application/json"}
        if sid:
            headers["Mcp-Session-Id"] = sid
        conn.request("POST", "/mcp/mcp", body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp

    try:
        a = post(INIT).getheader("Mcp-Session-Id")
        b = post(INIT).getheader("Mcp-Session-Id")
        check("two sessions open under the cap", {a, b} == FakeGateway.open, FakeGateway.open)
        post(CALL, a)  # a becomes the most recently used
        c = post(INIT).getheader("Mcp-Session-Id")
        check("third session closes the least recently used idle one (b)",
              FakeGateway.deleted == [b] and FakeGateway.open == {a, c}, (FakeGateway.deleted, FakeGateway.open))
        check("the evicted session gets 404, so its client re-initializes", post(CALL, b).status == 404)

        # a and c both busy: no room for a new session.
        slow = [threading.Thread(target=post, args=(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                                                 "params": {"name": "slow"}}).encode(), s))
                for s in (a, c)]
        for t in slow:
            t.start()
        time.sleep(0.4)
        refused = post(INIT)
        check("every session busy -> new session refused, 503 + Retry-After",
              refused.status == 503 and refused.getheader("Retry-After") == "5", refused.status)
        check("busy sessions never closed", FakeGateway.open == {a, c}, FakeGateway.open)
        for t in slow:
            t.join()
        check("once idle again, a new session is accepted", post(INIT).status == 200)
        check("never more than GATE_MAX_SESSIONS sessions upstream", len(FakeGateway.open) == 2, FakeGateway.open)

        conn = http.client.HTTPConnection("127.0.0.1", gate_port, timeout=15)
        sid = sorted(FakeGateway.open)[0]
        conn.request("DELETE", "/mcp/mcp", headers={"Cf-Access-Jwt-Assertion": token, "Mcp-Session-Id": sid})
        conn.getresponse().read()
        evicted = len(FakeGateway.deleted)
        d = post(INIT).getheader("Mcp-Session-Id")
        check("a session ended by its client frees its slot (no eviction needed)",
              d and len(FakeGateway.deleted) == evicted and len(FakeGateway.open) == 2,
              (FakeGateway.deleted, FakeGateway.open))
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
        upstream.shutdown()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
