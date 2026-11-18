"""Rate limiting.

`RateLimiter` is the seam: anything with `allow(key) -> (allowed, retry_after)`
works. The default is an in-process token bucket, which is correct for a single
worker. For several workers, drop in a Redis-backed limiter (for example one
built on an atomic Lua script) and assign it to `user_limiter` / `ip_limiter`.
"""
import math
import time
from typing import Callable, Protocol

from fastapi import Depends, HTTPException, Request

from .config import settings
from .security import current_user


class RateLimiter(Protocol):
    def allow(self, key: str) -> tuple[bool, float]: ...


class TokenBucket:
    def __init__(self, capacity: float, refill_per_sec: float, clock: Callable[[], float] = time.monotonic):
        self.capacity = capacity
        self.rate = refill_per_sec
        self.clock = clock
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_seen)

    def allow(self, key: str) -> tuple[bool, float]:
        # No awaits in here, so under asyncio this is atomic without a lock.
        now = self.clock()
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens >= 1:
            self._state[key] = (tokens - 1, now)
            return True, 0.0
        self._state[key] = (tokens, now)
        return False, (1 - tokens) / self.rate if self.rate > 0 else math.inf


user_limiter: RateLimiter = TokenBucket(settings.rl_user_capacity, settings.rl_user_refill_per_sec)
ip_limiter: RateLimiter = TokenBucket(settings.rl_ip_capacity, settings.rl_ip_refill_per_sec)


async def rate_limit(request: Request, user_id: str = Depends(current_user)) -> None:
    """Dependency for the hot endpoints: per-IP and per-user limits."""
    if not settings.rate_limit_enabled:
        return
    ip = request.client.host if request.client else "unknown"
    for limiter, key in ((ip_limiter, f"ip:{ip}"), (user_limiter, f"user:{user_id}")):
        allowed, retry_after = limiter.allow(key)
        if not allowed:
            raise HTTPException(
                429, "Too many requests",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
