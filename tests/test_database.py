"""Тесты слоя БД."""

from __future__ import annotations

import pytest

from bot.database import Database

pytestmark = pytest.mark.asyncio


async def test_upsert_and_get_user(db: Database) -> None:
    await db.upsert_user(111, "Shroud")
    user = await db.get_user(111)
    assert user is not None
    assert user["twitch_nick"] == "shroud"  # приводим к нижнему регистру


async def test_upsert_updates_nick(db: Database) -> None:
    await db.upsert_user(222, "alpha")
    await db.upsert_user(222, "beta")
    user = await db.get_user(222)
    assert user["twitch_nick"] == "beta"


async def test_find_user_by_nick(db: Database) -> None:
    await db.upsert_user(333, "Ninja")
    found = await db.find_user_by_nick("ninja")
    assert found is not None
    assert found["telegram_id"] == 333


async def test_delete_user(db: Database) -> None:
    await db.upsert_user(444, "gamma")
    await db.delete_user(444)
    assert await db.get_user(444) is None


async def test_add_and_get_clip(db: Database) -> None:
    await db.upsert_user(555, "delta")
    clip_id = await db.add_clip(555, "delta", "https://clips.twitch.tv/abc")
    clip = await db.get_clip(clip_id)
    assert clip is not None
    assert clip["clip_url"] == "https://clips.twitch.tv/abc"
    assert clip["user_id"] == 555


async def test_user_clips_ordering(db: Database) -> None:
    await db.upsert_user(666, "epsilon")
    await db.add_clip(666, "epsilon", "https://clips.twitch.tv/first")
    await db.add_clip(666, "epsilon", "https://clips.twitch.tv/second")
    clips = await db.user_clips(666)
    assert len(clips) == 2
    assert clips[0]["clip_url"] == "https://clips.twitch.tv/second"


async def test_update_user_twitch_auth_creates_user(db: Database) -> None:
    await db.update_user_twitch_auth(
        777,
        twitch_login="Streamer",
        access_token="access",
        refresh_token="refresh",
        twitch_user_id="12345",
    )

    user = await db.get_user(777)
    assert user is not None
    assert user["twitch_nick"] == "streamer"
    assert user["twitch_login"] == "streamer"
    assert user["twitch_access_token"] == "access"
    assert user["twitch_refresh_token"] == "refresh"
    assert user["twitch_user_id"] == "12345"

    found = await db.find_user_by_nick("STREAMER")
    assert found is not None
    assert found["telegram_id"] == 777
    assert found["twitch_access_token"] == "access"


async def test_clip_counters(db: Database) -> None:
    await db.upsert_user(1, "alpha")
    await db.upsert_user(2, "beta")
    await db.add_clip(1, "alpha", "https://clips.twitch.tv/a1")
    await db.add_clip(1, "alpha", "https://clips.twitch.tv/a2")
    await db.add_clip(2, "beta", "https://clips.twitch.tv/b1")

    assert await db.count_users() == 2
    assert await db.count_clips_total() == 3
    assert await db.count_clips(1) == 2
    assert await db.count_clips(2) == 1
    assert await db.count_clips(999) == 0


async def test_clips_per_channel(db: Database) -> None:
    await db.upsert_user(1, "alpha")
    await db.upsert_user(2, "beta")
    for _ in range(3):
        await db.add_clip(1, "alpha", "https://clips.twitch.tv/x")
    await db.add_clip(2, "beta", "https://clips.twitch.tv/y")

    top = await db.clips_per_channel()
    assert top[0]["twitch_nick"] == "alpha"
    assert top[0]["clip_count"] == 3
    assert top[1]["twitch_nick"] == "beta"
    assert top[1]["clip_count"] == 1


async def test_users_with_clip_counts(db: Database) -> None:
    await db.upsert_user(1, "alpha")
    await db.upsert_user(2, "beta")
    await db.add_clip(2, "beta", "https://clips.twitch.tv/y")

    rows = await db.users_with_clip_counts()
    by_nick = {r["twitch_nick"]: r for r in rows}
    assert by_nick["beta"]["clip_count"] == 1
    assert by_nick["beta"]["last_clip_at"] is not None
    assert by_nick["alpha"]["clip_count"] == 0
    # beta с клипом должна идти первой (ORDER BY clip_count DESC)
    assert rows[0]["twitch_nick"] == "beta"


async def test_count_clips_since(db: Database) -> None:
    await db.upsert_user(1, "alpha")
    await db.add_clip(1, "alpha", "https://clips.twitch.tv/x")
    # клип только что — попадает в окно "7 дней"
    assert await db.count_clips_since("2000-01-01 00:00:00") == 1
    # а в окно "с 2100 года" — нет
    assert await db.count_clips_since("2100-01-01 00:00:00") == 0


async def test_clip_duration_default(db: Database) -> None:
    # без записи в user_settings длительность — 60
    await db.upsert_user(888, "default_user")
    assert await db.get_clip_duration(888) == 60


async def test_clip_duration_unknown_user(db: Database) -> None:
    # пользователь вообще не существует — всё равно 60
    assert await db.get_clip_duration(424242) == 60


async def test_set_and_get_clip_duration(db: Database) -> None:
    await db.upsert_user(889, "setter")
    stored = await db.set_clip_duration(889, 30)
    assert stored == 30
    assert await db.get_clip_duration(889) == 30
    # повторная установка перезаписывает
    stored = await db.set_clip_duration(889, 15)
    assert stored == 15
    assert await db.get_clip_duration(889) == 15


async def test_clip_duration_clamped(db: Database) -> None:
    await db.upsert_user(890, "clamper")
    # ниже минимума -> 10
    assert await db.set_clip_duration(890, 5) == 10
    assert await db.get_clip_duration(890) == 10
    # выше максимума -> 60
    assert await db.set_clip_duration(890, 120) == 60
    assert await db.get_clip_duration(890) == 60
    # граничные значения проходят как есть
    assert await db.set_clip_duration(890, 10) == 10
    assert await db.set_clip_duration(890, 60) == 60


async def test_clip_duration_isolated_per_user(db: Database) -> None:
    await db.upsert_user(891, "user_a")
    await db.upsert_user(892, "user_b")
    await db.set_clip_duration(891, 20)
    # у второго пользователя осталась дефолтная
    assert await db.get_clip_duration(891) == 20
    assert await db.get_clip_duration(892) == 60


async def test_update_user_tokens(db: Database) -> None:
    """Обновление только токенов без затрагивания ника/логина."""
    await db.update_user_twitch_auth(
        900,
        twitch_login="tokuser",
        access_token="old_access",
        refresh_token="old_refresh",
        twitch_user_id="555",
    )
    await db.update_user_tokens(
        900,
        access_token="new_access",
        refresh_token="new_refresh",
        token_expires_at="9999999999",
    )
    user = await db.get_user(900)
    assert user is not None
    assert user["twitch_nick"] == "tokuser"
    assert user["twitch_access_token"] == "new_access"
    assert user["twitch_refresh_token"] == "new_refresh"
    assert user["twitch_token_expires_at"] == "9999999999"


async def test_update_user_twitch_auth_saves_expires_at(db: Database) -> None:
    """expires_at сохраняется при OAuth-флоу."""
    await db.update_user_twitch_auth(
        901,
        twitch_login="expuser",
        access_token="acc",
        refresh_token="ref",
        twitch_user_id="777",
        token_expires_at="1234567890",
    )
    user = await db.get_user(901)
    assert user is not None
    assert user["twitch_token_expires_at"] == "1234567890"


async def test_delete_old_clips(db: Database) -> None:
    """Клипы старше 24 ч удаляются, свежие остаются."""
    await db.upsert_user(1000, "cleanup_user")
    # вставим клип с датой 2 дня назад вручную
    conn = db._require_conn()
    await conn.execute(
        "INSERT INTO clips (user_id, twitch_nick, clip_url, created_at) VALUES (?, ?, ?, datetime('now', '-2 days'))",
        (1000, "cleanup_user", "https://clips.twitch.tv/old"),
    )
    await db.add_clip(1000, "cleanup_user", "https://clips.twitch.tv/fresh")
    await conn.commit()

    deleted = await db.delete_old_clips(hours=24)
    assert deleted == 1
    remaining = await db.user_clips(1000)
    assert len(remaining) == 1
    assert remaining[0]["clip_url"] == "https://clips.twitch.tv/fresh"


async def test_delete_old_clips_nothing_to_delete(db: Database) -> None:
    """Если старых клипов нет — возвращает 0."""
    await db.upsert_user(1001, "fresh_user")
    await db.add_clip(1001, "fresh_user", "https://clips.twitch.tv/new")
    assert await db.delete_old_clips(hours=24) == 0
    assert await db.count_clips(1001) == 1

