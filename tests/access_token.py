#!/usr/bin/env python3
"""Test helper: a fake Cloudflare Access team (RSA key + JWKS) and signed Access JWTs.

Uses the openssl command line for key generation and RS256 signing (no Python dependency).

    python3 tests/access_token.py init DIR            # DIR/access-key.pem + DIR/srv/certs (JWKS)
    python3 tests/access_token.py mint DIR EMAIL [--aud A] [--iss I] [--exp SECONDS] [--alg ALG]

As a module: Key(directory), key.jwks(), mint(key, email, ...).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time

DEFAULT_ISS = "https://e2e.cloudflareaccess.com"
DEFAULT_AUD = "e2e-aud"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Key:
    def __init__(self, directory: str, kid: str = "e2e-key"):
        os.makedirs(directory, exist_ok=True)
        self.kid = kid
        self.path = os.path.join(directory, "access-key.pem")
        if not os.path.exists(self.path):
            subprocess.run(["openssl", "genrsa", "-out", self.path, "2048"], check=True, capture_output=True)
        out = subprocess.run(["openssl", "rsa", "-in", self.path, "-noout", "-modulus"],
                             check=True, capture_output=True, text=True).stdout
        self.n = int(out.strip().split("=", 1)[1], 16)

    def jwks(self) -> dict:
        n = self.n.to_bytes((self.n.bit_length() + 7) // 8, "big")
        return {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": self.kid,
                          "n": b64url(n), "e": "AQAB"}]}

    def sign(self, data: bytes) -> bytes:
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.path],
                              input=data, check=True, capture_output=True).stdout


def mint(key: Key, email: str | None, aud=DEFAULT_AUD, iss=DEFAULT_ISS, exp: int = 600,
         alg: str = "RS256", kid: str | None = None, extra: dict | None = None) -> str:
    now = int(time.time())
    header = {"alg": alg, "kid": kid or key.kid, "typ": "JWT"}
    claims = {"aud": [aud] if isinstance(aud, str) else aud, "iss": iss, "iat": now, "nbf": now,
              "exp": now + exp, "sub": "e2e-user", "type": "app", "identity_nonce": "e2e"}
    if email:
        claims["email"] = email
    claims.update(extra or {})
    signed = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    signature = b"" if alg == "none" else key.sign(signed.encode())
    return f"{signed}.{b64url(signature)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("dir")
    p_mint = sub.add_parser("mint")
    p_mint.add_argument("dir")
    p_mint.add_argument("email")
    p_mint.add_argument("--aud", default=DEFAULT_AUD)
    p_mint.add_argument("--iss", default=DEFAULT_ISS)
    p_mint.add_argument("--exp", type=int, default=600)
    p_mint.add_argument("--alg", default="RS256")
    args = parser.parse_args()

    key = Key(args.dir)
    if args.cmd == "init":
        srv = os.path.join(args.dir, "srv")
        os.makedirs(srv, exist_ok=True)
        with open(os.path.join(srv, "certs"), "w") as fh:
            json.dump(key.jwks(), fh)
        os.chmod(srv, 0o755)
        os.chmod(os.path.join(srv, "certs"), 0o644)
        return 0
    print(mint(key, args.email, aud=args.aud, iss=args.iss, exp=args.exp, alg=args.alg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
