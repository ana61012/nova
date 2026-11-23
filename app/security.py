import base64
import hashlib
import hmac
import secrets
import time
import uuid

import jwt
from fastapi import Header, HTTPException

from .config import settings

def hash_code(email: str, code: str) -> str:
    """Keyed hash of a sign-in code. A 6-digit code is trivially brute-forced if the
    table leaks, so the server secret is what protects it, together with the short
    expiry and attempt cap. The "otp:" prefix keeps this use of the key separate from
    JWT signing."""
    return hmac.new(settings.jwt_secret.encode(), f"otp:{email}:{code}".encode(), hashlib.sha256).hexdigest()


# ---- account passwords (scrypt, stdlib only) ------------------------------------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=64 * 1024 * 1024)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A missing/garbled hash still burns the same CPU, so response
    time does not reveal whether an address has a password."""
    try:
        _, n, r, p, salt, digest = (stored or "").split("$")
        salt_b, want = base64.b64decode(salt), base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=salt_b, n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except Exception:
        hashlib.scrypt(password.encode(), salt=b"\0" * 16, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=64 * 1024 * 1024)
        return False


# Fresh on every process start. Tokens carry it, so a restart invalidates every
# token issued before it and everyone has to sign in again. This is only correct
# while a single process serves requests (see the Dockerfile); with several workers
# each would have its own id and reject the others' tokens.
BOOT_ID = uuid.uuid4().hex


def create_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + settings.jwt_ttl_seconds, "bid": BOOT_ID},
        settings.jwt_secret, algorithm="HS256",
    )


def create_admin_token() -> str:
    """Dashboard token. The "role" claim keeps it from working as a shop token (and
    shop tokens from working on the dashboard); it also dies on server restart."""
    now = int(time.time())
    return jwt.encode(
        {"sub": "admin", "role": "admin", "iat": now, "exp": now + settings.admin_jwt_ttl_seconds, "bid": BOOT_ID},
        settings.jwt_secret, algorithm="HS256",
    )


def _claims(token: str) -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if claims.get("bid") != BOOT_ID:  # issued before the last server restart
        raise HTTPException(401, "Invalid or expired token")
    return claims


def user_from_token(token: str) -> str:
    claims = _claims(token)
    if claims.get("role") is not None:  # an admin token is not a shopper
        raise HTTPException(401, "Invalid or expired token")
    return claims["sub"]


def check_admin_credentials(username: str, password: str) -> bool:
    # Compare both fields in constant time and never short-circuit between them.
    ok_user = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    ok_pass = hmac.compare_digest(password.encode(), settings.admin_password.encode())
    return ok_user and ok_pass


async def require_admin(authorization: str | None = Header(None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    if _claims(authorization[7:].strip()).get("role") != "admin":
        raise HTTPException(401, "Invalid or expired token")


async def current_user(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return user_from_token(authorization[7:].strip())