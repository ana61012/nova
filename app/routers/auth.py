"""Shop sign-in. Two ways in, both returning the same JWT:

    POST /api/auth/signup        {email, password}  -> 201 {token, user_id, email}   (create account)
    POST /api/auth/login         {email, password}  -> 200 {token, user_id, email}

Or passwordless: email a one-time code, exchange it for a JWT.

    POST /api/auth/request-code  {email}        -> 202 (always, whether or not the address is known)
    POST /api/auth/verify-code   {email, code}  -> 200 {token, user_id, email}
    POST /api/admin/login        {username, password} -> 200 {token, expires_in}   (dashboard only)

With the code flow there is no separate signup: the first successful verification creates the account.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..ratelimit import limit_auth
from ..models import User
from ..schemas import AdminLoginIn, AdminTokenOut, CodeSentOut, EmailIn, LoginIn, PasswordIn, TokenOut, VerifyIn
from ..security import check_admin_credentials, create_admin_token, create_token, hash_password, verify_password
from ..services import mailer, otp

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/request-code", response_model=CodeSentOut, status_code=202)
async def request_code(body: EmailIn, request: Request, session: AsyncSession = Depends(get_session)):
    limit_auth(request)
    email = body.email.lower()
    try:
        code = await otp.issue_code(session, email)
    except otp.Cooldown as c:
        raise HTTPException(429, "Please wait before asking for another code",
                            headers={"Retry-After": str(c.retry_after)})
    try:
        await mailer.mailer.send_code(email, code, settings.otp_ttl_seconds // 60)
    except Exception as exc:  # never tell the caller whether delivery worked for this address
        print(f"mail error for {email}: {exc!r}", flush=True)
    return CodeSentOut(
        detail="If that address can receive email, a code is on its way.",
        expires_in=settings.otp_ttl_seconds, resend_after=settings.otp_resend_cooldown_seconds,
    )


@router.post("/verify-code", response_model=TokenOut)
async def verify_code(body: VerifyIn, request: Request, session: AsyncSession = Depends(get_session)):
    limit_auth(request)
    email = body.email.lower()
    user = await otp.verify_code(session, email, body.code)
    # One message for wrong, expired, used, locked-out and unknown-address alike.
    if user is None:
        raise HTTPException(401, "Invalid or expired code")
    return TokenOut(token=create_token(user.id), user_id=user.id, email=email)


@router.post("/signup", response_model=TokenOut, status_code=201)
async def signup(body: PasswordIn, request: Request, session: AsyncSession = Depends(get_session)):
    limit_auth(request)
    email = body.email.lower()
    taken = HTTPException(409, "An account with this email already exists. Try logging in.")
    if (await session.execute(select(User.id).where(User.email == email))).first():
        raise taken
    user = User(email=email, password_hash=await asyncio.to_thread(hash_password, body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # two signups raced on the same address
        await session.rollback()
        raise taken
    return TokenOut(token=create_token(user.id), user_id=user.id, email=email)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    limit_auth(request)
    email = body.email.lower()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    ok = await asyncio.to_thread(verify_password, body.password, user.password_hash if user else None)
    if user is None or not ok:
        raise HTTPException(401, "Wrong email or password")  # same answer for unknown address and bad password
    return TokenOut(token=create_token(user.id), user_id=user.id, email=email)


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_router.post("/login", response_model=AdminTokenOut)
async def admin_login(body: AdminLoginIn, request: Request):
    limit_auth(request)  # same tight per-IP bucket as the shop sign-in: this takes password guesses
    if not check_admin_credentials(body.username, body.password):
        raise HTTPException(401, "Wrong username or password")
    return AdminTokenOut(token=create_admin_token(), expires_in=settings.admin_jwt_ttl_seconds)


if settings.dev_mode and settings.email_backend.lower() == "memory":
    # Lets the load test and demos read the 'email' without an inbox. It exists only
    # when BOTH dev mode and the in-memory mailer are on.
    @router.get("/dev/mailbox", include_in_schema=False)
    async def dev_mailbox(email: str):
        code = getattr(mailer.mailer, "latest", {}).get(email.lower())
        if code is None:
            raise HTTPException(404, "No code sent to that address")
        return {"email": email.lower(), "code": code}