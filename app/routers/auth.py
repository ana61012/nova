import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User
from ..schemas import Credentials, TokenOut
from ..security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=TokenOut, status_code=201)
async def signup(body: Credentials, session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    # scrypt is CPU-heavy: run it in a thread so it cannot stall the event loop.
    pw_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(email=email, password_hash=pw_hash)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Email already registered")
    return TokenOut(token=create_token(user.id), user_id=user.id, email=email)


@router.post("/login", response_model=TokenOut)
async def login(body: Credentials, session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    # Same error for "no such user" and "wrong password" so emails cannot be probed.
    ok = user is not None and await asyncio.to_thread(verify_password, body.password, user.password_hash)
    if not ok:
        raise HTTPException(401, "Invalid email or password")
    return TokenOut(token=create_token(user.id), user_id=user.id, email=email)
