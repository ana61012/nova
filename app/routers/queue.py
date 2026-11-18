import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_session
from ..ratelimit import rate_limit
from ..security import current_user, user_from_token
from ..services import queue

router = APIRouter(prefix="/api/queue", tags=["queue"])


async def _drop_id(session: AsyncSession) -> int:
    drop_id = await queue.current_drop_id(session)
    if drop_id is None:
        raise HTTPException(404, "No drop scheduled")
    return drop_id


@router.post("/join")
async def join(
    user_id: str = Depends(current_user), _: None = Depends(rate_limit),
    session: AsyncSession = Depends(get_session),
):
    drop_id = await _drop_id(session)
    await queue.join(session, drop_id, user_id)
    return await queue.status_payload(session, drop_id, user_id)


@router.post("/leave")
async def leave(user_id: str = Depends(current_user), session: AsyncSession = Depends(get_session)):
    drop_id = await _drop_id(session)
    await queue.leave(session, drop_id, user_id)
    return await queue.status_payload(session, drop_id, user_id)


@router.get("/status")
async def status(user_id: str = Depends(current_user), session: AsyncSession = Depends(get_session)):
    drop_id = await _drop_id(session)
    return await queue.status_payload(session, drop_id, user_id)


@router.get("/stream")
async def stream(request: Request, token: str):
    """Server-sent events with live queue position.

    Browsers' EventSource cannot set an Authorization header, so this one
    endpoint takes the token as a query parameter.
    """
    user_id = user_from_token(token)

    async def events():
        last = None
        while not await request.is_disconnected():
            async with SessionLocal() as session:
                drop_id = await queue.current_drop_id(session)
                payload = await queue.status_payload(session, drop_id, user_id) if drop_id else {"state": "none"}
            if payload != last:
                yield f"data: {json.dumps(payload)}\n\n"
                last = payload
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
