"""
Точка входа бота.

Поднимает:
  - aiosqlite (БД)
  - aiohttp-сессию для скачивания клипов
  - TwitchAPI (Helix)
  - Twitch IRC-листенер (слушает !clip в чатах привязанных стримеров)
  - aiogram Dispatcher (Long Polling)

Всё живёт в одном asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .chat_listener import ChatListener
from .clip_service import ClipResult, ClipService
from .config import Config, ConfigError, load_config
from .database import Database
from .handlers import notify_admin, router, send_clip_result
from .twitch_api import TwitchAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("twitchclipbot")


async def main() -> None:
    try:
        config: Config = load_config()
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        sys.exit(1)

    # 1) База данных
    db = Database(config.db_path)
    await db.connect()

    # 2) HTTP-сессия для скачивания видео
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=180),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )

    # 3) Twitch Helix API
    api = TwitchAPI(config.twitch_client_id, config.twitch_client_secret)
    await api.start()

    # 4) Сервис создания клипов
    clip_service = ClipService(
        api=api,
        db=db,
        clips_dir=config.clips_dir,
        width=config.video_width,
        height=config.video_height,
        wait_timeout=config.clip_wait_timeout,
        poll_interval=config.clip_poll_interval,
    )

    # 5) Telegram bot / dispatcher
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # Прокидываем зависимости в хендлеры через workflow_data
    dp["db"] = db
    dp["clip_service"] = clip_service
    dp["http_session"] = http_session
    dp["config"] = config

    # 6) IRC-листенер
    async def on_clip(channel: str) -> None:
        """
        Вызывается при получении !clip в чате канала.

        Находит Telegram-пользователя, привязавшего этот Twitch-ник,
        создаёт клип и рассылает его пользователю и админу.
        """
        user = await db.find_user_by_nick(channel)
        if user is None:
            log.info("Для канала %s нет привязанного Telegram-пользователя", channel)
            return

        user_id = int(user["telegram_id"])
        result: ClipResult = await clip_service.create_clip_for_user(
            user_id, channel, http_session
        )
        await send_clip_result(bot, user_id, result, db=db)
        await notify_admin(bot, config.admin_id, result)

    listener = ChatListener(
        bot_nick=config.twitch_client_id,
        oauth_token="",  # читаем чат анонимно через justinfan
        on_clip=on_clip,
        anonymous=True,
    )
    dp["listener"] = listener

    # Подписываем слушателя на все уже привязанные каналы
    for u in await db.all_users():
        listener.subscribe(u["twitch_nick"])
    await listener.start()

    # 7) Запуск Telegram polling
    log.info("Бот запущен. Админ: %s. Каналов на прослушке: %d", config.admin_id, len(listener.channels))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаю бота…")
        await listener.stop()
        await api.close()
        await http_session.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Завершено пользователем")
