"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# asyncpg + pgbouncer (transaction mode, used by Supabase's pooler) are
# incompatible with asyncpg's prepared-statement cache — pgbouncer
# multiplexes connections per-transaction, so the prepared statement
# from one transaction may end up on a different backend the next time.
# Setting `statement_cache_size=0` disables the cache and keeps every
# query as a simple-protocol query, which pgbouncer handles fine.
# Harmless for non-pooled local Postgres too.
engine = create_async_engine(
    str(settings.database_url),
    echo=settings.database_echo,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    connect_args={
        "statement_cache_size": 0,
        # Supabase's transaction-mode pooler applies a short default
        # statement_timeout that kills our long search/scrape flow. Bump it
        # per-session via asyncpg's server_settings (sent in the startup
        # packet, respected through pgbouncer). Value is in milliseconds.
        "server_settings": {"statement_timeout": "180000"},
    },
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session per request."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
