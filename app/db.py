from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        # Wait for the write lock instead of failing under concurrent writers.
        kwargs["connect_args"] = {"timeout": 30}
    eng = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return eng


engine = make_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session


async def init_db(eng=None):
    from . import models  # noqa: F401  (register tables)

    async with (eng or engine).begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
