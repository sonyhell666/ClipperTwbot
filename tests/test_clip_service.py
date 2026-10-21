"""Тесты ClipService._get_valid_token — рефреш токена."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.clip_service import ClipService
from bot.database import Database

pytestmark = pytest.mark.asyncio


def _make_service(db: Database) -> ClipService:
    api = MagicMock()
    return ClipService(
        api=api,
        db=db,
        clips_dir="/tmp",
        client_id="cid",
        client_secret="secret",
    )


async def test_get_valid_token_not_expired(db: Database) -> None:
    """Токен ещё валиден — рефреш не вызывается."""
    await db.update_user_twitch_auth(
        1,
        twitch_login="user",
        access_token="valid_token",
        refresh_token="ref",
        token_expires_at=str(time.time() + 10000),
    )
    user_row = await db.get_user(1)
    svc = _make_service(db)
    with patch("bot.clip_service._oauth_refresh", new_callable=AsyncMock) as mock_ref:
        token = await svc._get_valid_token(1, user_row)
        assert token == "valid_token"
        mock_ref.assert_not_called()


async def test_get_valid_token_refreshes_when_expired(db: Database) -> None:
    """Токен истёк — вызывается рефреш и результат сохраняется в БД."""
    await db.update_user_twitch_auth(
        2,
        twitch_login="user2",
        access_token="old_token",
        refresh_token="my_refresh",
        token_expires_at=str(time.time() - 100),  # истёк
    )
    user_row = await db.get_user(2)
    svc = _make_service(db)

    mock_response = {
        "access_token": "new_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 14400,
    }
    with patch(
        "bot.clip_service._oauth_refresh", new_callable=AsyncMock, return_value=mock_response
    ) as mock_ref:
        token = await svc._get_valid_token(2, user_row)
        assert token == "new_access_token"
        mock_ref.assert_called_once_with("my_refresh", "cid", "secret")

    # проверяем, что БД обновилась
    updated = await db.get_user(2)
    assert updated["twitch_access_token"] == "new_access_token"
    assert updated["twitch_refresh_token"] == "new_refresh_token"


async def test_get_valid_token_returns_old_when_refresh_fails(db: Database) -> None:
    """Если рефреш упал — возвращаем старый токен (fallback)."""
    await db.update_user_twitch_auth(
        3,
        twitch_login="user3",
        access_token="old_token",
        refresh_token="my_refresh",
        token_expires_at=str(time.time() - 100),
    )
    user_row = await db.get_user(3)
    svc = _make_service(db)

    with patch(
        "bot.clip_service._oauth_refresh",
        new_callable=AsyncMock,
        side_effect=RuntimeError("refresh failed"),
    ):
        token = await svc._get_valid_token(3, user_row)
        assert token == "old_token"


async def test_get_valid_token_no_access_token(db: Database) -> None:
    """Нет access_token — возвращаем None."""
    await db.upsert_user(4, "noauth")
    user_row = await db.get_user(4)
    svc = _make_service(db)
    token = await svc._get_valid_token(4, user_row)
    assert token is None


async def test_get_valid_token_refreshes_when_no_expiry_info(db: Database) -> None:
    """Нет данных об истечении — рефрешим превентивно (старые записи до миграции)."""
    await db.update_user_twitch_auth(
        5,
        twitch_login="user5",
        access_token="some_token",
        refresh_token="some_refresh",
        token_expires_at="",
    )
    user_row = await db.get_user(5)
    svc = _make_service(db)
    mock_response = {
        "access_token": "refreshed_token",
        "refresh_token": "new_ref",
        "expires_in": 14400,
    }
    with patch(
        "bot.clip_service._oauth_refresh", new_callable=AsyncMock, return_value=mock_response
    ) as mock_ref:
        token = await svc._get_valid_token(5, user_row)
        assert token == "refreshed_token"
        mock_ref.assert_called_once()


async def test_get_valid_token_no_refresh_token_no_expiry(db: Database) -> None:
    """Нет refresh_token и нет expires_at — возвращаем старый access_token как есть."""
    await db.update_user_twitch_auth(
        6,
        twitch_login="user6",
        access_token="bare_token",
        refresh_token="",
        token_expires_at="",
    )
    user_row = await db.get_user(6)
    svc = _make_service(db)
    with patch("bot.clip_service._oauth_refresh", new_callable=AsyncMock) as mock_ref:
        token = await svc._get_valid_token(6, user_row)
        assert token == "bare_token"
        mock_ref.assert_not_called()
