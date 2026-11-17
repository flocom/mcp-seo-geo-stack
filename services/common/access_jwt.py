"""Vérification des jetons Cloudflare Access (en-tête Cf-Access-Jwt-Assertion).

Bibliothèque standard uniquement. Un jeton n'est accepté que si :
  - il est signé en RS256 par l'une des clés publiques de l'équipe Access
    (https://<équipe>.cloudflareaccess.com/cdn-cgi/access/certs) ;
  - son émetteur (iss) est https://<équipe>.cloudflareaccess.com ;
  - son audience (aud) contient l'AUD de l'application Access ;
  - il n'est pas expiré (exp) ni pas encore valide (nbf), à 60 s près.

Sans ACCESS_TEAM_DOMAIN et ACCESS_AUD, tout est refusé (échec fermé). Contrairement à
l'en-tête Cf-Access-Authenticated-User-Email, un tel jeton ne peut pas être fabriqué par
un visiteur, même si une route est publiée sans Cloudflare Access devant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request


def _team(value: str) -> str:
    value = value.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/")


TEAM_DOMAIN = _team(os.environ.get("ACCESS_TEAM_DOMAIN", ""))
AUDIENCES = [a.strip() for a in os.environ.get("ACCESS_AUD", "").split(",") if a.strip()]
ISSUER = f"https://{TEAM_DOMAIN}" if TEAM_DOMAIN else ""
# Surchargeable uniquement pour les tests (serveur de clés local).
CERTS_URL = os.environ.get("ACCESS_CERTS_URL") or (f"{ISSUER}/cdn-cgi/access/certs" if TEAM_DOMAIN else "")
LEEWAY = 60
KEYS_TTL = 3600          # relecture normale des clés
KEYS_MIN_REFRESH = 30    # au plus une relecture forcée (clé inconnue) toutes les 30 s

# DigestInfo DER de SHA-256 (RFC 8017, EMSA-PKCS1-v1_5).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class AccessError(Exception):
    """Jeton absent, invalide ou non vérifiable : la requête doit être refusée."""


def configured() -> bool:
    return bool(TEAM_DOMAIN and AUDIENCES)


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


_keys: dict[str, tuple[int, int]] = {}
_fetched_at = 0.0
_keys_lock = threading.Lock()


def _fetch_keys() -> dict[str, tuple[int, int]]:
    with urllib.request.urlopen(CERTS_URL, timeout=10) as resp:
        data = json.load(resp)
    keys = {}
    for jwk in data.get("keys", []):
        if jwk.get("kty") == "RSA" and jwk.get("kid") and jwk.get("n") and jwk.get("e"):
            keys[jwk["kid"]] = (int.from_bytes(_b64decode(jwk["n"]), "big"),
                                int.from_bytes(_b64decode(jwk["e"]), "big"))
    return keys


def _public_key(kid: str) -> tuple[int, int]:
    global _keys, _fetched_at
    with _keys_lock:
        now = time.time()
        stale = now - _fetched_at > KEYS_TTL
        unknown = kid not in _keys and now - _fetched_at > KEYS_MIN_REFRESH
        if stale or unknown:
            _fetched_at = now  # même en cas d'échec : pas de rafale de requêtes
            try:
                _keys = _fetch_keys()
            except (OSError, ValueError) as exc:
                if not _keys:
                    raise AccessError(f"clés publiques Cloudflare Access indisponibles ({exc})") from None
        if kid not in _keys:
            raise AccessError("jeton signé par une clé inconnue")
        return _keys[kid]


def _rsa_sha256_valid(signed: bytes, signature: bytes, n: int, e: int) -> bool:
    size = (n.bit_length() + 7) // 8
    if len(signature) != size or size < 256:  # clés de 2048 bits minimum
        return False
    sig = int.from_bytes(signature, "big")
    if sig >= n:
        return False
    em = pow(sig, e, n).to_bytes(size, "big")
    digest = _SHA256_DIGEST_INFO + hashlib.sha256(signed).digest()
    expected = b"\x00\x01" + b"\xff" * (size - 3 - len(digest)) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


def verify(token: str) -> dict:
    """Retourne les claims d'un jeton valide, lève AccessError sinon."""
    if not configured():
        raise AccessError("Cloudflare Access n'est pas configuré (ACCESS_TEAM_DOMAIN et ACCESS_AUD dans .env)")
    if not token:
        raise AccessError("jeton Cloudflare Access absent (en-tête Cf-Access-Jwt-Assertion)")
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_b64decode(head_b64))
        claims = json.loads(_b64decode(body_b64))
        signature = _b64decode(sig_b64)
    except ValueError:
        raise AccessError("jeton Cloudflare Access illisible") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AccessError("jeton Cloudflare Access illisible")
    if header.get("alg") != "RS256":
        raise AccessError("algorithme de signature refusé")
    n, e = _public_key(str(header.get("kid", "")))
    if not _rsa_sha256_valid(f"{head_b64}.{body_b64}".encode(), signature, n, e):
        raise AccessError("signature du jeton invalide")

    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + LEEWAY:
        raise AccessError("jeton expiré")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + LEEWAY < nbf:
        raise AccessError("jeton pas encore valide")
    if claims.get("iss") != ISSUER:
        raise AccessError("jeton émis par une autre équipe Cloudflare Access")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not any(isinstance(a, str) and a in AUDIENCES for a in auds):
        raise AccessError("jeton destiné à une autre application Access (aud)")
    return claims


def email_from(token: str) -> str:
    """Adresse e-mail vérifiée du jeton (les jetons de service, sans adresse, sont refusés)."""
    email = verify(token).get("email")
    if not isinstance(email, str) or "@" not in email:
        raise AccessError("jeton sans adresse e-mail (jeton de service refusé)")
    return email.strip()
