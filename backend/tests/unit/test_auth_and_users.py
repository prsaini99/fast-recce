"""Unit tests for AuthService + UserService (M9)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.schemas.auth import UserCreate, UserUpdate
from app.services.auth_service import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.services.user_service import UserService

pytestmark = pytest.mark.asyncio


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://u:p@localhost/x",
        jwt_secret_key="super-secret-key-for-tests-only",
        google_places_api_key="x",
        gemini_api_key="x",
    )


# --- Password hashing ---


def test_hash_and_verify_password_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_hash_tolerates_long_passwords() -> None:
    # bcrypt caps at 72 bytes — should not raise.
    long_pw = "x" * 200
    hashed = hash_password(long_pw)
    assert verify_password(long_pw, hashed) is True


# --- JWT ---


def test_access_token_roundtrip() -> None:
    settings = _settings()
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(
        user_id=user_id, email="a@b.com", role="reviewer", settings=settings
    )
    assert expires_in == settings.access_token_expire_minutes * 60

    claims = decode_token(token, settings=settings)
    assert claims is not None
    assert claims.user_id == user_id
    assert claims.email == "a@b.com"
    assert claims.role == "reviewer"
    assert claims.token_type == "access"


def test_refresh_token_is_distinct_type() -> None:
    settings = _settings()
    user_id = uuid.uuid4()
    refresh = create_refresh_token(
        user_id=user_id, email="a@b.com", role="admin", settings=settings
    )
    claims = decode_token(refresh, settings=settings)
    assert claims is not None
    assert claims.token_type == "refresh"


def test_tampered_token_returns_none() -> None:
    settings = _settings()
    token, _ = create_access_token(
        user_id=uuid.uuid4(), email="a@b.com", role="viewer", settings=settings
    )
    tampered = token[:-4] + "zzzz"
    assert decode_token(tampered, settings=settings) is None


# --- UserService ---


async def test_create_and_get_user(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    user = await service.create(
        UserCreate(
            email="reviewer@fastrecce.com",
            password="pass12345",
            full_name="Rev",
            role="reviewer",
        )
    )
    fetched = await service.get(user.id)
    assert fetched.email == "reviewer@fastrecce.com"


async def test_duplicate_email_raises_conflict(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    await service.create(
        UserCreate(
            email="a@fastrecce.com",
            password="pass12345",
            full_name="A",
            role="viewer",
        )
    )
    with pytest.raises(ConflictError):
        await service.create(
            UserCreate(
                email="A@FastRecce.com",  # case-insensitive uniqueness
                password="pass12345",
                full_name="A2",
                role="viewer",
            )
        )


async def test_authenticate_happy_path(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    await service.create(
        UserCreate(
            email="x@fastrecce.com",
            password="correctpass",
            full_name="X",
            role="reviewer",
        )
    )
    user = await service.authenticate("X@fastrecce.com", "correctpass")
    assert user.role == "reviewer"


async def test_authenticate_wrong_password(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    await service.create(
        UserCreate(
            email="x@fastrecce.com",
            password="correctpass",
            full_name="X",
            role="reviewer",
        )
    )
    with pytest.raises(UnauthorizedError):
        await service.authenticate("x@fastrecce.com", "wrong")


async def test_authenticate_unknown_email(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    with pytest.raises(UnauthorizedError):
        await service.authenticate("nope@fastrecce.com", "whatever")


async def test_authenticate_inactive_user_blocked(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    user = await service.create(
        UserCreate(
            email="x@fastrecce.com",
            password="correctpass",
            full_name="X",
            role="reviewer",
        )
    )
    await service.update(user.id, UserUpdate(is_active=False))
    with pytest.raises(UnauthorizedError):
        await service.authenticate("x@fastrecce.com", "correctpass")


async def test_get_missing_user_raises(db_session: AsyncSession) -> None:
    service = UserService(db=db_session)
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())
