"""
Клиент Twitch Helix API.

Умеет:
  - получать app access token (client_credentials);
  - проверять, идёт ли стрим у указанного канала;
  - создавать клип на активном стриме;
  - ожидать готовности клипа (у Twitch требуется несколько секунд на обработку);
  - получать URL и метаданные готового клипа.

Все методы асинхронные и бросают TwitchAPIError при проблемах.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

OAUTH_URL = "https://id.twitch.tv/oauth2/token"
HELIX_BASE = "https://api.twitch.tv/helix"


class TwitchAPIError(RuntimeError):
    """Любая ошибка при обращении к Twitch Helix API."""


class StreamOfflineError(TwitchAPIError):
    """Стрим оффлайн — клип создать нельзя."""


class ClipNotReadyError(TwitchAPIError):
    """Клип не успел обработаться за отведённое время."""


@dataclass
class Clip:
    """Полезные данные клипа."""

    id: str
    url: str
    edit_url: str
    thumbnail_url: str
    creator_name: str
    broadcaster_name: str
    title: str
    duration: float

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Clip":
        return cls(
            id=data.get("id", ""),
            url=data.get("url", ""),
            edit_url=data.get("edit_url", ""),
            thumbnail_url=data.get("thumbnail_url", ""),
            creator_name=data.get("creator_name", ""),
            broadcaster_name=data.get("broadcaster_name", ""),
            title=data.get("title", ""),
            duration=float(data.get("duration", 0) or 0),
        )


class TwitchAPI:
    """Обёртка над Helix с автообновлением токена."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id и client_secret обязательны")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "TwitchAPI":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---------- токен ----------

    async def _fetch_token(self) -> None:
        await self.start()
        assert self._session is not None
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }
        try:
            async with self._session.post(OAUTH_URL, params=params) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    raise TwitchAPIError(
                        f"Не удалось получить токен Twitch: HTTP {resp.status} {payload}"
                    )
        except aiohttp.ClientError as exc:
            raise TwitchAPIError(f"Сеть недоступна при получении токена: {exc}") from exc

        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token:
            raise TwitchAPIError(f"Twitch не вернул access_token: {payload}")
        self._token = token
        # немного запаса, чтобы не поймать 401 на границе
        self._token_expires_at = time.time() + max(expires_in - 60, 60)
        log.info("Twitch app-token получен, истекает через %s сек", expires_in)

    async def _ensure_token(self) -> None:
        async with self._lock:
            if self._token is None or time.time() >= self._token_expires_at:
                await self._fetch_token()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        """Заголовки Helix. Если передан token — используем его вместо app token."""
        auth_token = token or self._token
        if not auth_token:
            raise TwitchAPIError("Нет access_token: сначала вызовите _ensure_token")
        return {
            "Client-Id": self._client_id,
            "Authorization": f"Bearer {auth_token}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        user_token: str | None = None,
    ) -> dict[str, Any]:
        """Универсальный запрос к Helix с ретраем на 401 (обновление токена)."""
        await self.start()
        assert self._session is not None
        url = f"{HELIX_BASE}{path}"

        if user_token:
            headers = self._headers(user_token)
        else:
            await self._ensure_token()
            headers = self._headers()

        for attempt in (1, 2):
            try:
                async with self._session.request(
                    method, url, params=params, json=json_body, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401 and attempt == 1 and not user_token:
                        log.warning("Twitch вернул 401, обновляю токен")
                        self._token = None
                        self._token_expires_at = 0.0
                        await self._ensure_token()
                        headers = self._headers()
                        continue
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001
                        data = {"raw": text}
                    if resp.status >= 400:
                        raise TwitchAPIError(
                            f"Twitch API {method} {path} -> HTTP {resp.status}: {data}"
                        )
                    return data
            except aiohttp.ClientError as exc:
                if attempt == 2:
                    raise TwitchAPIError(f"Сетевая ошибка Twitch API: {exc}") from exc
                await asyncio.sleep(1)
        raise TwitchAPIError("Не удалось выполнить запрос к Twitch API")

    # ---------- бизнес-логика ----------

    async def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        """Возвращает объект пользователя по логину или None."""
        data = await self._request("GET", "/users", params={"login": login.lower()})
        items = data.get("data") or []
        return items[0] if items else None

    async def is_stream_live(self, login: str) -> bool:
        """Проверяет, идёт ли стрим у канала (в статусе live)."""
        data = await self._request("GET", "/streams", params={"user_login": login.lower()})
        return bool(data.get("data"))

    async def create_clip(self, broadcaster_login: str, access_token: str) -> str:
        """
        Создаёт клип на активном стриме.

        access_token — User OAuth token стримера с правом clips:edit.
        Возвращает id созданного клипа.
        """
        user = await self.get_user_by_login(broadcaster_login)
        if user is None:
            raise TwitchAPIError(f"Twitch-канал '{broadcaster_login}' не найден")

        if not await self.is_stream_live(broadcaster_login):
            raise StreamOfflineError(
                f"Канал {broadcaster_login} сейчас оффлайн — клип создать нельзя"
            )

        data = await self._request(
            "POST",
            "/clips",
            params={"broadcaster_id": user["id"]},
            user_token=access_token,
        )
        items = data.get("data") or []
        if not items:
            raise TwitchAPIError("Twitch не вернул данные созданного клипа")
        clip_id = items[0].get("id")
        if not clip_id:
            raise TwitchAPIError(f"Twitch не вернул id клипа: {items}")
        log.info("Клип создан, id=%s (broadcaster=%s)", clip_id, broadcaster_login)
        return clip_id

    async def get_clip(self, clip_id: str) -> Clip | None:
        """Возвращает готовый клип по id или None, если ещё не обработан."""
        data = await self._request("GET", "/clips", params={"id": clip_id})
        items = data.get("data") or []
        if not items:
            return None
        return Clip.from_json(items[0])

    async def wait_for_clip(
        self,
        clip_id: str,
        *,
        timeout: int = 30,
        interval: int = 5,
    ) -> Clip:
        """
        Циклично опрашивает Helix, пока клип не обработается.

        Twitch обычно отдаёт готовый клип через 5-20 секунд после создания.
        """
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                clip = await self.get_clip(clip_id)
            except TwitchAPIError as exc:
                last_error = str(exc)
                log.warning("Ошибка при получении клипа %s: %s", clip_id, exc)
            else:
                if clip is not None:
                    log.info("Клип %s готов: %s", clip_id, clip.url)
                    return clip
            await asyncio.sleep(interval)
        raise ClipNotReadyError(
            f"Клип {clip_id} не обработался за {timeout} сек"
            + (f" (последняя ошибка: {last_error})" if last_error else "")
        )
