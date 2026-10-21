"""Async engine and sessions. PostgreSQL for production, SQLite for development/tests."""
import asyncio
import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_async_engine(
    settings.database_url,
    **({} if _is_sqlite else dict(pool_size=10, max_overflow=20, pool_pre_ping=True)),
)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


log = logging.getLogger("smartolt.db")


async def wait_for_db(eng=None, retries: int = 15, delay: float = 2.0) -> None:
    """Wait for the database and report a clear error (URL without password) instead of a long traceback."""
    eng, last = eng or engine, None
    for i in range(retries):
        try:
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:          # Rejected connection, incorrect password, missing database, etc.
            last = exc
            if i == 0:
                log.warning("Database is not reachable yet (%s: %s); retrying up to %d times…", exc.__class__.__name__, exc, retries)
            await asyncio.sleep(delay)
    url = eng.url.render_as_string(hide_password=True)
    raise RuntimeError(f"Could not connect to database {url} — {last.__class__.__name__}: {last}. "
                       "Ensure PostgreSQL is running (systemctl status postgresql), the host and port in DATABASE_URL are correct, "
                       "and that the database user, password, and database have been created.") from last


def ensure_columns(sync_conn) -> list[str]:
    """Lightweight migration: add missing columns to existing tables without removing or changing data.
    New columns receive their Python defaults so existing rows are populated."""
    from sqlalchemy import inspect, literal, text
    insp, added = inspect(sync_conn), []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=sync_conn.dialect)}'
            if col.default is not None and col.default.is_scalar:
                lit = literal(col.default.arg, col.type).compile(
                    dialect=sync_conn.dialect, compile_kwargs={"literal_binds": True})
                ddl += f" DEFAULT {lit}"
            sync_conn.execute(text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


async def init_db() -> None:
    import app.models  # noqa: F401  (register tables)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_columns)
    from app.security import seed_admin
    async with SessionLocal() as session:
        await seed_admin(session)


async def get_session():
    async with SessionLocal() as session:
        yield session
