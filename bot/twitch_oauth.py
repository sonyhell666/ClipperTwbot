"""
Утилиты для OAuth-флоу Twitch.

Используются и в OAuth-сервере (callback на 8080), и в Telegram-хендлерах,
где пользователь вставляет ссылку с code после авторизации на localhost.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .twitch_api import HELIX_BASE, OAUTH_URL

log = logging.getLogger(__name__)


async def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Меняет authorization code на access_token + refresh_token."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            OAUTH_URL,
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Twitch OAuth error: {payload}")
            return payload


async def fetch_login(access_token: str, client_id: str) -> tuple[str, str]:
    """Возвращает (login, user_id) по access_token."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{HELIX_BASE}/users",
            headers={
                "Client-Id": client_id,
                "Authorization": f"Bearer {access_token}",
            },
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Не удалось получить логин: {payload}")
            data = payload.get("data") or []
            if not data:
                raise RuntimeError("Twitch не вернул данные пользователя")
            return data[0]["login"], data[0]["id"]


async def send_telegram(bot_token: str, chat_id: int, text: str) -> None:
    """Отправляет сообщение в Telegram через Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить Telegram-уведомление: %s", exc)
