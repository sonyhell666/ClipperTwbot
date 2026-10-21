"""
Конфигурация бота.

Все секреты и настройки загружаются из переменных окружения (.env).
Ничего не хардкодим — только чтение окружения с валидацией.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Загружаем .env из корня проекта (если файл есть)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(RuntimeError):
    """Ошибка конфигурации: отсутствует обязательная переменная окружения."""


@dataclass(frozen=True)
class Config:
    """Иммутабельный контейнер настроек приложения."""

    bot_token: str
    twitch_client_id: str
    twitch_client_secret: str
    admin_id: int
    db_path: Path
    clips_dir: Path
    clip_wait_timeout: int = 30       # максимум секунд ожидания готовности клипа
    clip_poll_interval: int = 5        # интервал опроса Twitch Helix в секундах
    video_width: int = 1080
    video_height: int = 1920
    oauth_redirect_uri: str = "http://localhost"


_REQUIRED_KEYS = (
    "BOT_TOKEN",
    "TWITCH_CLIENT_ID",
    "TWITCH_CLIENT_SECRET",
    "ADMIN_ID",
)


def _get_required(name: str) -> str:
    """Возвращает обязательную переменную окружения или бросает ConfigError."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Не задана обязательная переменная окружения: {name}")
    return value.strip()


def _get_int(name: str) -> int:
    """Читает целочисленную переменную окружения с внятной ошибкой."""
    raw = _get_required(name)
    try:
        return int(raw)
    except ValueError as exc:  # noqa: PERF203
        raise ConfigError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def load_config() -> Config:
    """
    Собирает и валидирует конфигурацию из окружения.

    Бросает ConfigError, если чего-то не хватает — это лучше, чем упасть
    где-то посреди обработки апдейта.
    """
    missing = [key for key in _REQUIRED_KEYS if not os.getenv(key, "").strip()]
    if missing:
        raise ConfigError(
            "Отсутствуют обязательные переменные окружения: " + ", ".join(missing)
        )

    data_dir = BASE_DIR / "data"
    clips_dir = data_dir / "clips"
    data_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=_get_required("BOT_TOKEN"),
        twitch_client_id=_get_required("TWITCH_CLIENT_ID"),
        twitch_client_secret=_get_required("TWITCH_CLIENT_SECRET"),
        admin_id=_get_int("ADMIN_ID"),
        db_path=data_dir / "bot.sqlite3",
        clips_dir=clips_dir,
        clip_wait_timeout=int(os.getenv("CLIP_WAIT_TIMEOUT", "30")),
        clip_poll_interval=int(os.getenv("CLIP_POLL_INTERVAL", "5")),
        video_width=int(os.getenv("VIDEO_WIDTH", "1080")),
        video_height=int(os.getenv("VIDEO_HEIGHT", "1920")),
        oauth_redirect_uri=os.getenv("OAUTH_REDIRECT_URI", "http://localhost").strip(),
    )
