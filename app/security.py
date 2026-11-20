import hashlib
import hmac
import time

import jwt
from fastapi import Header, HTTPException

from .config import settings

def hash_code(email: str, code: str) -> str:
    """Keyed hash of a sign-in code. A 6-digit code is trivially brute-forced if the
    table leaks, so the server secret is what protects it, together with the short
    expiry and attempt cap. The "otp:" prefix keeps this use of the key separate from
    JWT signing."""
    return hmac.new(settings.jwt_secret.encode(), f"otp:{email}:{code}".encode(), hashlib.sha256).hexdigest()


def create_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + settings.jwt_ttl_seconds},
        settings.jwt_secret, algorithm="HS256",
    )


def user_from_token(token: str) -> str:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")


async def current_user(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return user_from_token(authorization[7:].strip())