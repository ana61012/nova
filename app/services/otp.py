"""One-time email codes.

Every limit is a conditional UPDATE (the same pattern as stock and orders), so
concurrent requests cannot beat them:
  * guesses:   attempts += 1 only WHERE attempts < max, then compare
  * single use: consumed_at is set only WHERE consumed_at IS NULL; rowcount == 1 wins
  * expiry:    checked in the same WHERE clause
"""
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import LoginCode, User, utcnow
from ..security import hash_code


class Cooldown(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


async def issue_code(session: AsyncSession, email: str) -> str:
    """Create a fresh code for `email` and retire any older ones. Returns the plaintext
    code, which exists only long enough to be emailed."""
    now = utcnow()
    recent = (await session.execute(
        select(LoginCode.id)
        .where(LoginCode.email == email,
               LoginCode.created_at > now - timedelta(seconds=settings.otp_resend_cooldown_seconds))
        .limit(1)
    )).first()
    if recent:
        raise Cooldown(settings.otp_resend_cooldown_seconds)

    # Each code allows a few guesses, so unlimited new codes would mean unlimited guesses.
    last_hour = (await session.execute(
        select(func.count()).select_from(LoginCode)
        .where(LoginCode.email == email, LoginCode.created_at > now - timedelta(hours=1))
    )).scalar_one()
    if last_hour >= settings.otp_max_codes_per_hour:
        raise Cooldown(3600)

    await session.execute(
        update(LoginCode).where(LoginCode.email == email, LoginCode.consumed_at.is_(None)).values(consumed_at=now)
    )
    code = f"{secrets.randbelow(10**6):06d}"  # CSPRNG, zero-padded
    session.add(LoginCode(
        email=email, code_hash=hash_code(email, code),
        created_at=now, expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
    ))
    await session.commit()
    return code


async def verify_code(session: AsyncSession, email: str, code: str) -> User | None:
    """Return the user (created on first sign-in) if `code` is right, else None.
    Callers must give the same answer for every failure reason."""
    now = utcnow()
    row = (await session.execute(
        select(LoginCode)
        .where(LoginCode.email == email, LoginCode.consumed_at.is_(None), LoginCode.expires_at > now)
        .order_by(LoginCode.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None

    # Spend a guess before looking at the answer.
    spent = await session.execute(
        update(LoginCode)
        .where(LoginCode.id == row.id, LoginCode.consumed_at.is_(None), LoginCode.expires_at > now,
               LoginCode.attempts < settings.otp_max_attempts)
        .values(attempts=LoginCode.attempts + 1)
    )
    await session.commit()
    if spent.rowcount != 1 or not hmac.compare_digest(row.code_hash, hash_code(email, code)):
        return None

    # Single use: of any concurrent correct submissions, exactly one flips consumed_at.
    used = await session.execute(
        update(LoginCode).where(LoginCode.id == row.id, LoginCode.consumed_at.is_(None)).values(consumed_at=now)
    )
    await session.commit()
    if used.rowcount != 1:
        return None

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:  # two first-time sign-ins raced; the other one created it
            await session.rollback()
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    return user