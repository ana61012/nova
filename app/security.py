import hashlib
import hmac
import os
import time

import jwt
from fastapi import Header, HTTPException

from .config import settings

# scrypt parameters are deliberately modest so demos and tests stay quick.
# Raise n (to 2**15 or more) for anything real.
_SCRYPT = dict(n=2**12, r=8, p=1)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, dk_hex = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(dk.hex(), dk_hex)


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
