"""Passwort-Hashing (Argon2) und JWT. Tokens sind an diesen Server gebunden (aud = Server-ID)."""
from datetime import datetime, timedelta, timezone

import secrets

import jwt
from pwdlib import PasswordHash

from app.config import get_settings

_hasher = PasswordHash.recommended()
# Wird geprüft, wenn es den Benutzer nicht gibt – gleiche Rechenzeit, damit man
# an der Antwortzeit nicht erkennt, ob ein Benutzername existiert.
_DUMMY_HASH = _hasher.hash("dummy-passwort-zum-zeitausgleich")

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False
    return _hasher.verify(password, password_hash)


def create_token(user_id: str, token_version: int, audience: str) -> tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=s.jwt_expire_days)
    token = jwt.encode(
        {"sub": user_id, "ver": token_version, "aud": audience, "jti": secrets.token_urlsafe(12),
         "iat": int(now.timestamp()), "exp": int(expires.timestamp())},
        s.jwt_secret,
        algorithm=ALGORITHM,
    )
    return token, expires.replace(microsecond=0)


def token_jti(token: str) -> str:
    """Kennung eines (schon geprüften) Tokens – für „dieses Gerät bleibt angemeldet“."""
    try:
        return str(jwt.decode(token, options={"verify_signature": False}).get("jti") or "")
    except jwt.PyJWTError:
        return ""


def decode_token(token: str, audience: str) -> tuple[str, int] | None:
    """Gibt (User-ID, Token-Version) zurück oder None, wenn das Token ungültig, abgelaufen oder fremd ist."""
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM], audience=audience)
    except jwt.PyJWTError:
        return None
    sub, ver = payload.get("sub"), payload.get("ver", 0)
    if not isinstance(sub, str) or not isinstance(ver, int):
        return None
    return sub, ver
