"""
Сервисный слой: полный цикл создания клипа.

Объединяет Twitch API, скачивание, ffmpeg и запись в БД. Не зависит от Telegram,
что позволяет тестировать логику отдельно и переиспользовать в IRC-листенере.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .database import Database
from .twitch_api import (
    Clip,
    ClipNotReadyError,
    StreamOfflineError,
    TwitchAPI,
    TwitchAPIError,
)
from .twitch_oauth import refresh_token as _oauth_refresh
from .video import VideoError, download_clip, make_vertical

log = logging.getLogger(__name__)


@dataclass
class ClipResult:
    """Результат обработки запроса на клип."""

    success: bool
    user_id: int
    twitch_nick: str
    clip_url: str = ""
    video_path: Path | None = None
    error: str = ""


class ClipService:
    """Высокоуровневый сервис: создать клип -> скачать -> сделать вертикальным -> сохранить."""

    def __init__(
        self,
        api: TwitchAPI,
        db: Database,
        clips_dir: Path,
        *,
        width: int = 1080,
        height: int = 1920,
        wait_timeout: int = 30,
        poll_interval: int = 5,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        self._api = api
        self._db = db
        self._clips_dir = clips_dir
        self._width = width
        self._height = height
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval
        self._client_id = client_id
        self._client_secret = client_secret

    async def _download_and_process(
        self,
        clip: Clip,
        session: aiohttp.ClientSession,
        *,
        duration: int | None = None,
    ) -> Path:
        """Скачивает и конвертирует клип; возвращает путь к вертикальному файлу."""
        token = uuid.uuid4().hex[:12]
        raw_path = self._clips_dir / f"{clip.id}_{token}_src.mp4"
        vertical_path = self._clips_dir / f"{clip.id}_{token}_vertical.mp4"

        await download_clip(session, clip.url, raw_path)
        result = await make_vertical(
            raw_path,
            vertical_path,
            width=self._width,
            height=self._height,
            duration=duration,
        )
        # сырой файл удаляем, если конвертация удалась и файлы различаются
        if result != raw_path and raw_path.exists():
            try:
                raw_path.unlink()
            except OSError as exc:  # noqa: BLE001
                log.debug("Не удалось удалить исходный клип: %s", exc)
        return result

    async def _get_valid_token(
        self, telegram_id: int, user_row: dict
    ) -> str | None:
        """
        Возвращает рабочий access_token пользователя.
        Если токен истёк или скоро истечёт — пробует рефрешить.
        Возвращает None, если рефреш невозможен.
        """
        access_token = user_row.get("twitch_access_token") or ""
        refresh_tok = user_row.get("twitch_refresh_token") or ""
        expires_raw = user_row.get("twitch_token_expires_at") or ""

        if not access_token:
            return None

        # Если знаем время истечения — рефрешим заранее (за 5 минут)
        if expires_raw:
            try:
                expires_at = float(expires_raw)
                if time.time() < expires_at - 300:
                    return access_token
            except (TypeError, ValueError):
                pass  # нечисловое значение — пробуем рефрешить

        if not refresh_tok or not self._client_id or not self._client_secret:
            log.warning(
                "Нет refresh_token или client_id/secret для telegram_id=%s", telegram_id
            )
            return access_token  # всё равно вернём — может ещё работать

        try:
            token_data = await _oauth_refresh(
                refresh_tok, self._client_id, self._client_secret
            )
        except Exception as exc:
            log.warning(
                "Не удалось рефрешить токен telegram_id=%s: %s", telegram_id, exc
            )
            return access_token  # fallback на старый

        new_access = token_data.get("access_token", "")
        new_refresh = token_data.get("refresh_token", refresh_tok)
        expires_in = int(token_data.get("expires_in", 14400))
        new_expires_at = str(time.time() + expires_in)

        try:
            await self._db.update_user_tokens(
                telegram_id,
                access_token=new_access,
                refresh_token=new_refresh,
                token_expires_at=new_expires_at,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить новые токены в БД: %s", exc)

        log.info("Токен рефрешнут для telegram_id=%s", telegram_id)
        return new_access

    async def create_clip_for_user(
        self,
        user_id: int,
        twitch_nick: str,
        session: aiohttp.ClientSession,
    ) -> ClipResult:
        """
        Полный цикл: создать клип, дождаться готовности, скачать, сделать 9:16.

        Берёт OAuth-токен стримера из БД — без него Twitch не даст создать клип.
        Никогда не бросает исключение — возвращает ClipResult с полем error.
        """
        # Получаем токен стримера из БД
        user_row = await self._db.find_user_by_nick(twitch_nick)
        if user_row is None:
            return ClipResult(
                False,
                user_id,
                twitch_nick,
                error=(
                    "Twitch-аккаунт не авторизован. "
                    "Нажми «Подключить Twitch» в боте и пройди авторизацию."
                ),
            )

        telegram_id = int(user_row["telegram_id"])
        access_token = await self._get_valid_token(telegram_id, user_row)
        if not access_token:
            return ClipResult(
                False,
                user_id,
                twitch_nick,
                error=(
                    "Twitch-аккаунт не авторизован. "
                    "Нажми «Подключить Twitch» в боте и пройди авторизацию."
                ),
            )

        try:
            clip_id = await self._api.create_clip(twitch_nick, access_token=access_token)
        except StreamOfflineError as exc:
            log.warning("Стрим оффлайн (%s): %s", twitch_nick, exc)
            return ClipResult(False, user_id, twitch_nick, error=str(exc))
        except TwitchAPIError as exc:
            log.error("Ошибка Twitch API при создании клипа (%s): %s", twitch_nick, exc)
            return ClipResult(
                False, user_id, twitch_nick, error=f"Ошибка Twitch API: {exc}"
            )

        try:
            clip = await self._api.wait_for_clip(
                clip_id,
                timeout=self._wait_timeout,
                interval=self._poll_interval,
            )
        except ClipNotReadyError as exc:
            log.warning("Клип не готов: %s", exc)
            # даже если не готов — отдадим хотя бы ссылку на страницу клипа
            fallback_url = f"https://clips.twitch.tv/{clip_id}"
            await self._db.add_clip(user_id, twitch_nick, fallback_url)
            return ClipResult(
                False,
                user_id,
                twitch_nick,
                clip_url=fallback_url,
                error=str(exc),
            )
        except TwitchAPIError as exc:
            return ClipResult(
                False, user_id, twitch_nick, error=f"Ошибка Twitch API: {exc}"
            )

        # персональная длительность клипа из настроек пользователя
        try:
            duration = await self._db.get_clip_duration(user_id)
        except Exception:  # noqa: BLE001
            duration = None

        try:
            video_path = await self._download_and_process(clip, session, duration=duration)
        except (VideoError, asyncio.TimeoutError) as exc:
            log.error("Ошибка обработки видео для клипа %s: %s", clip.id, exc)
            # клип есть, но скачать/обработать не получилось — отдадим только ссылку
            await self._db.add_clip(user_id, twitch_nick, clip.url)
            return ClipResult(
                False,
                user_id,
                twitch_nick,
                clip_url=clip.url,
                error=f"Клип создан, но видео не обработалось: {exc}",
            )

        await self._db.add_clip(user_id, twitch_nick, clip.url)
        return ClipResult(
            True,
            user_id,
            twitch_nick,
            clip_url=clip.url,
            video_path=video_path,
        )
