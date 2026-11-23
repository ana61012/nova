from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str, poolclass=None):
    kwargs = {}
    if url.startswith("sqlite"):
        # Wait for the write lock instead of failing under concurrent writers.
        kwargs["connect_args"] = {"timeout": 30}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
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
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    """create_all never alters an existing table, so a database created before a column
    was added (e.g. users.password_hash) would crash on every query. Add the column
    in place; existing rows get NULL."""
    from sqlalchemy import inspect, text

    insp = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have and col.nullable and not col.primary_key:
                ddl = col.type.compile(dialect=sync_conn.dialect)
                sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}'))