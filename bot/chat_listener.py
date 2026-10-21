"""
Twitch IRC-листенер.

Подключается к IRC-чату Twitch через websocket напрямую (без twitchio),
чтобы держать зависимости под контролем и не тянуть лишнее.

Слушает команду !clip в чате стримера, чей ник привязан к Telegram-пользователю,
и вызывает переданный async-колбэк.

Реализация использует тот же app access token, что и Helix API
(для чтения чата нужен scope, выдаваемый вместе с client_credentials).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
CLIP_COMMAND = "!clip"
RECONNECT_DELAY = 5.0
PING_TIMEOUT = 240.0


ClipCallback = Callable[[str], Awaitable[None]]


@dataclass
class ChatListener:
    """
    Подключается к IRC Twitch и слушает сообщения в каналах.

    Использование:
        listener = ChatListener(nick, token, on_clip)
        listener.subscribe("shroud")
        await listener.run()   # блокирующий цикл
    """

    bot_nick: str
    oauth_token: str
    on_clip: ClipCallback
    anonymous: bool = True
    _channels: set[str] = field(default_factory=set)
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _reconnect: asyncio.Event = field(default_factory=asyncio.Event)

    def subscribe(self, twitch_nick: str) -> None:
        """Добавляет канал в список прослушиваемых и обновляет JOIN."""
        nick = twitch_nick.strip().lower()
        if nick and nick not in self._channels:
            self._channels.add(nick)
            self._reconnect.set()
            log.info("Подписка на канал: %s", nick)

    def unsubscribe(self, twitch_nick: str) -> None:
        nick = twitch_nick.strip().lower()
        self._channels.discard(nick)

    @property
    def channels(self) -> set[str]:
        return set(self._channels)

    async def start(self) -> None:
        """Запускает фоновый цикл переподключения."""
        self._stop.clear()
        self._reconnect.clear()
        self._task = asyncio.create_task(self._run_forever(), name="twitch-irc")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._reconnect.clear()
            try:
                await self._session_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("IRC-сессия упала: %s. Переподключение через %.0fс", exc, RECONNECT_DELAY)
            if self._stop.is_set():
                break
            await asyncio.sleep(RECONNECT_DELAY)

    async def _session_loop(self) -> None:
        """Одна IRC-сессия: connect -> JOIN -> слушаем -> reconnect при обрыве."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=PING_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(IRC_URL, heartbeat=PING_TIMEOUT) as ws:
                log.info("IRC подключён, авторизуюсь как %s", self.bot_nick)
                if self.anonymous:
                    await ws.send_str(f"NICK justinfan{abs(hash(self.bot_nick)) % 100000}\r\n")
                else:
                    await ws.send_str(f"PASS oauth:{self.oauth_token}\r\n")
                    await ws.send_str(f"NICK {self.bot_nick}\r\n")
                await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")

                joined: set[str] = set()

                # JOIN всех каналов
                for ch in list(self._channels):
                    await ws.send_str(f"JOIN #{ch}\r\n")
                    joined.add(ch)

                while not self._stop.is_set():
                    read_task = asyncio.create_task(
                        ws.receive(), name="twitch-irc-receive"
                    )
                    reconnect_task = asyncio.create_task(
                        self._reconnect.wait(), name="twitch-irc-reconnect"
                    )
                    try:
                        done, pending = await asyncio.wait(
                            {read_task, reconnect_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if read_task in pending:
                            read_task.cancel()
                        if reconnect_task in pending:
                            reconnect_task.cancel()
                        for task in pending:
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                        if reconnect_task in done and self._reconnect.is_set():
                            self._reconnect.clear()
                            for ch in sorted(self._channels - joined):
                                log.info("IRC JOIN #%s", ch)
                                await ws.send_str(f"JOIN #{ch}\r\n")
                                joined.add(ch)
                            continue

                        msg = read_task.result() if read_task in done else None
                        if msg is None:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSING,
                            ):
                                log.warning("IRC websocket закрыт: %s", msg.type)
                                break
                            continue
                        for line in msg.data.split("\r\n"):
                            await self._handle_line(line, ws)
                    finally:
                        if not read_task.done():
                            read_task.cancel()
                        if not reconnect_task.done():
                            reconnect_task.cancel()

    async def _handle_line(self, line: str, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Обрабатывает одну строку IRC."""
        if not line:
            return
        if line.startswith("PING"):
            await ws.send_str("PONG :tmi.twitch.tv\r\n")
            return
        if " PRIVMSG " not in line:
            return

        try:
            prefix, rest = line[1:].split(" ", 1) if line.startswith(":") else ("", line)
            nick = prefix.split("!", 1)[0]
            _, params = rest.split(" PRIVMSG ", 1)
            channel_part, message = params.split(" :", 1)
            channel = channel_part.lstrip("#").lower()
        except ValueError:
            return

        text = message.strip()
        if text.lower().startswith(CLIP_COMMAND):
            log.info("Команда !clip от %s в #%s", nick, channel)
            asyncio.create_task(self._safe_on_clip(channel))

    async def _safe_on_clip(self, channel: str) -> None:
        try:
            await self.on_clip(channel)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка в обработчике on_clip: %s", exc)
