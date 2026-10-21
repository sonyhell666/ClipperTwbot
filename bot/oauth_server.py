"""
OAuth-callback-сервер для авторизации стримеров.

Twitch редиректит сюда с ?code=...&state=... после того, как стример нажал
«Авторизоваться». Сервер меняет code на access_token, получает логин
пользователя через /users и сохраняет всё в БД.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import web

from .database import Database
from .twitch_api import HELIX_BASE, OAUTH_URL

log = logging.getLogger(__name__)

HTML_SUCCESS = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Авторизация успешна</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0e0e10; color: #efeff1; display: flex; align-items: center;
               justify-content: center; min-height: 100vh; margin: 0; }
        .card { text-align: center; padding: 2rem; background: #18181b;
                border-radius: 12px; max-width: 420px; }
        h1 { color: #a970ff; margin-top: 0; }
        p { color: #adadb8; line-height: 1.5; }
        a { color: #a970ff; }
    </style>
</head>
<body>
    <div class="card">
        <h1>✅ Twitch подключён!</h1>
        <p>Аккаунт <b>%(login)s</b> привязан к боту.<br>
        Теперь в чате стримера пиши <code>!clip</code> — клип придёт в Telegram.</p>
        <p>Эту страницу можно закрыть и вернуться в Telegram.</p>
    </div>
</body>
</html>"""

HTML_ERROR = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Ошибка авторизации</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0e0e10; color: #efeff1; display: flex; align-items: center;
               justify-content: center; min-height: 100vh; margin: 0; }
        .card { text-align: center; padding: 2rem; background: #18181b;
                border-radius: 12px; max-width: 420px; }
        h1 { color: #e91916; margin-top: 0; }
        p { color: #adadb8; line-height: 1.5; }
    </style>
</head>
<body>
    <div class="card">
        <h1>❌ Ошибка</h1>
        <p>%(message)s</p>
        <p>Вернись в Telegram и попробуй снова.</p>
    </div>
</body>
</html>"""


class OAuthServer:
    """Принимает callback от Twitch и завершает авторизацию."""

    def __init__(
        self,
        *,
        db: Database,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        notify_chat_id: int | None = None,
        bot_token: str | None = None,
    ) -> None:
        self._db = db
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._notify_chat_id = notify_chat_id
        self._bot_token = bot_token
        self._app = web.Application()
        self._app.router.add_get("/callback", self._handle_callback)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Поднимает HTTP-сервер."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        log.info("OAuth callback-сервер слушает на %s:%d", host, port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            log.warning("OAuth ошибка от Twitch: %s — %s", error, request.query.get("error_description"))
            return web.Response(
                text=HTML_ERROR % {"message": f"Twitch вернул ошибку: {error}"},
                content_type="text/html",
                status=400,
            )

        if not code or not state:
            return web.Response(
                text=HTML_ERROR % {"message": "Отсутствует code или state в запросе."},
                content_type="text/html",
                status=400,
            )

        telegram_id = await self._db.consume_oauth_state(state)
        if telegram_id is None:
            log.warning("OAuth callback с неизвестным state=%s", state)
            return web.Response(
                text=HTML_ERROR % {"message": "Ссылка устарела или уже использована. Запроси новую в боте."},
                content_type="text/html",
                status=400,
            )

        try:
            token_data = await self._exchange_code(code)
            login = await self._fetch_login(token_data["access_token"])
            await self._db.update_user_twitch_auth(
                telegram_id,
                twitch_login=login,
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", ""),
                twitch_user_id=token_data.get("user_id", ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка при обмене OAuth-кода для telegram_id=%s", telegram_id)
            return web.Response(
                text=HTML_ERROR % {"message": f"Не удалось завершить авторизацию: {exc}"},
                content_type="text/html",
                status=500,
            )

        log.info("OAuth успешен: telegram_id=%s -> twitch=%s", telegram_id, login)

        if self._bot_token:
            await self._send_telegram(
                telegram_id,
                f"✅ Twitch <b>{login}</b> подключён!\n\n"
                "Теперь в чате стримера пиши <code>!clip</code> — клип придёт сюда.",
            )
        if self._notify_chat_id:
            await self._send_telegram(
                self._notify_chat_id,
                f"🔔 Новый стример подключён: <b>{login}</b> (tg_id={telegram_id})",
            )

        return web.Response(
            text=HTML_SUCCESS % {"login": login},
            content_type="text/html",
        )

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        """Меняет authorization code на access_token."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OAUTH_URL,
                params={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self._redirect_uri,
                },
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"Twitch OAuth error: {payload}")
                return payload

    async def _fetch_login(self, access_token: str) -> str:
        """Получает логин пользователя по access_token."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HELIX_BASE}/users",
                headers={
                    "Client-Id": self._client_id,
                    "Authorization": f"Bearer {access_token}",
                },
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"Не удалось получить логин: {payload}")
                data = payload.get("data") or []
                if not data:
                    raise RuntimeError("Twitch не вернул данные пользователя")
                return data[0]["login"]

    async def _send_telegram(self, chat_id: int, text: str) -> None:
        """Отправляет сообщение в Telegram через Bot API."""
        if not self._bot_token:
            return
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
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
