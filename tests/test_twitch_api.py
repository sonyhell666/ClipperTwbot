"""
Тесты Twitch API-клиента с моком сети.

Мы не бьём по реальному Twitch — вместо этого подменяем aiohttp.ClientSession
на фейковую реализацию, которая отдаёт подготовленные ответы.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.twitch_api import (
    ClipNotReadyError,
    StreamOfflineError,
    TwitchAPI,
    TwitchAPIError,
)

pytestmark = pytest.mark.asyncio


# ---------- фейковый HTTP-слой ----------


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)


class FakeSession:
    """Отвечает заранее заданными ответами в порядке поступления запросов."""

    closed = False

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses

    def post(self, url: str, params: dict | None = None) -> FakeResponse:
        return self._responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


TOKEN_OK = FakeResponse(200, {"access_token": "tok", "expires_in": 3600})


async def _api_with(responses: list[FakeResponse]) -> TwitchAPI:
    api = TwitchAPI("cid", "secret")
    await api.start()
    api._session = FakeSession([TOKEN_OK, *responses])  # noqa: SLF001
    return api


# ---------- тесты ----------


async def test_get_user_by_login_found() -> None:
    api = await _api_with(
        [FakeResponse(200, {"data": [{"id": "123", "login": "shroud"}]})]
    )
    user = await api.get_user_by_login("shroud")
    assert user == {"id": "123", "login": "shroud"}
    await api.close()


async def test_get_user_by_login_not_found() -> None:
    api = await _api_with([FakeResponse(200, {"data": []})])
    assert await api.get_user_by_login("nope") is None
    await api.close()


async def test_is_stream_live_true() -> None:
    api = await _api_with(
        [FakeResponse(200, {"data": [{"id": "1", "user_login": "shroud"}]})]
    )
    assert await api.is_stream_live("shroud") is True
    await api.close()


async def test_create_clip_requires_online_stream() -> None:
    # 1: /users ok, 2: /streams пусто -> StreamOfflineError
    api = await _api_with(
        [
            FakeResponse(200, {"data": [{"id": "123", "login": "shroud"}]}),
            FakeResponse(200, {"data": []}),
        ]
    )
    with pytest.raises(StreamOfflineError):
        await api.create_clip("shroud", access_token="fake")
    await api.close()


async def test_create_clip_success() -> None:
    # /users -> /streams -> POST /clips
    api = await _api_with(
        [
            FakeResponse(200, {"data": [{"id": "123", "login": "shroud"}]}),
            FakeResponse(200, {"data": [{"id": "1"}]}),
            FakeResponse(200, {"data": [{"id": "clip123", "edit_url": "x"}]}),
        ]
    )
    clip_id = await api.create_clip("shroud", access_token="fake")
    assert clip_id == "clip123"
    await api.close()


async def test_wait_for_clip_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    api = await _api_with(
        [
            FakeResponse(200, {"data": []}),
            FakeResponse(200, {"data": []}),
        ]
    )
    # Уменьшаем таймаут, чтобы тест был мгновенным
    monkeypatch.setattr("bot.twitch_api.asyncio.sleep", lambda _s: _noop())
    with pytest.raises(ClipNotReadyError):
        await api.wait_for_clip("clip123", timeout=0, interval=0)
    await api.close()


async def test_wait_for_clip_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    api = await _api_with(
        [
            FakeResponse(200, {"data": []}),
            FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "clip123",
                            "url": "https://clips.twitch.tv/clip123",
                            "edit_url": "https://clips.twitch.tv/clip123/edit",
                            "thumbnail_url": "https://example/thumb.jpg",
                            "creator_name": "u",
                            "broadcaster_name": "shroud",
                            "title": "cool",
                            "duration": 30.5,
                        }
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr("bot.twitch_api.asyncio.sleep", lambda _s: _noop())
    clip = await api.wait_for_clip("clip123", timeout=30, interval=0)
    assert clip.url == "https://clips.twitch.tv/clip123"
    assert clip.broadcaster_name == "shroud"
    await api.close()


async def _noop() -> None:
    return None
