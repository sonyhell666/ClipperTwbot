"""
Обработка видео клипов: скачивание и приведение к вертикальному формату 9:16 (1080x1920).

Twitch отдаёт клипы в 16:9. Чтобы получить вертикальный формат, используется
каскад ffmpeg-фильтров: масштабирование с сохранением пропорций, заливка фона
размытой версией видео и финальный кроп по центру.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from urllib.parse import quote



import aiohttp

log = logging.getLogger(__name__)


class VideoError(RuntimeError):
    """Ошибка скачивания или обработки видео."""



def _is_mp4_file(path: Path) -> bool:
    """Проверяет, что файл начинается с типичного MP4/ISOBMFF-заголовка."""
    try:
        head = path.read_bytes()[:32]
        return b"ftyp" in head or b"moov" in head or b"mdat" in head
    except OSError:
        return False


async def _resolve_clip_download_url(session: aiohttp.ClientSession, clip_url: str) -> str:
    """
    Пытается получить прямой URL видеофайла через публичный GQL-запрос Twitch.
    Возвращает исходный clip_url, если GQL не сработал.
    """
    try:
        slug = clip_url.rstrip("/").split("/")[-1]
        gql = [
            {
                "operationName": "ShareClipRenderStatus",
                "variables": {"slug": slug},
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "0a02bb974443b576f5579aab0fef1d4b7f44e58a8a256f0c5adfead0db70640f",
                    }
                },
            }
        ]
        async with session.post(
            "https://gql.twitch.tv/gql",
            json=gql,
            headers={"Client-ID": "kimne78kx3ncx6brgo4mv6wki5h1ko"},
        ) as resp:
            if resp.status != 200:
                return clip_url
            data = await resp.json()
            clip = (data or [{}])[0].get("data", {}).get("clip") or {}
            token = clip.get("playbackAccessToken") or {}
            sig = token.get("signature") or ""
            tok = token.get("value") or ""
            # Предпочитаем h264 (совместимость), иначе hevc из assets
            qualities = clip.get("videoQualities") or []
            if not qualities:
                assets = clip.get("assets") or []
                for asset in assets:
                    if asset.get("aspectRatio", 0) > 1:  # LANDSCAPE
                        qualities = asset.get("videoQualities") or []
                        if qualities:
                            break
            if qualities:
                # В токене clip_uri указывает на конкретное качество — используем его
                if sig and tok:
                    try:
                        import json as _json
                        tok_data = _json.loads(tok)
                        clip_uri = tok_data.get("clip_uri")
                        if clip_uri:
                            url = clip_uri
                        else:
                            url = qualities[0].get("sourceURL")
                    except Exception:
                        url = qualities[0].get("sourceURL")
                    if url:
                        return f"{url}?sig={sig}&token={quote(tok)}"
                url = qualities[0].get("sourceURL")
                if url:
                    return url
    except Exception as exc:  # noqa: BLE001
        log.debug("GQL clip URL resolution failed for %s: %s", clip_url, exc)
    return clip_url



async def download_clip(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    chunk_size: int = 1 << 16,
) -> Path:
    """Скачивает клип по URL в файл dest и проверяет, что это MP4, а не HTML."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    download_url = await _resolve_clip_download_url(session, url)
    log.info("Download URL: %s", download_url)
    try:
        async with session.get(download_url) as resp:
            if resp.status != 200:
                raise VideoError(f"Не удалось скачать клип: HTTP {resp.status}")
            with dest.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    fh.write(chunk)
    except aiohttp.ClientError as exc:
        raise VideoError(f"Сетевая ошибка при скачивании клипа: {exc}") from exc

    if dest.stat().st_size == 0:
        raise VideoError("Скачанный файл клипа пуст")
    if not _is_mp4_file(dest):
        head = dest.read_bytes()[:200].decode("utf-8", errors="replace")
        raise VideoError(
            f"Скачанный файл не похож на видео (возможно, HTML-страница). "
            f"Первые байты: {head!r}"
        )
    log.info("Клип скачан: %s (%d байт)", dest, dest.stat().st_size)
    return dest


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _run(cmd: list[str]) -> None:
    """Запускает внешнюю команду и бросает VideoError при ненулевом коде возврата."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-2000:]
        raise VideoError(f"ffmpeg завершился с кодом {proc.returncode}: {tail}")


def build_vertical_filter(width: int = 1080, height: int = 1920) -> str:
    """
    Формирует строку фильтров ffmpeg для вертикального формата 9:16.

    Логика:
      1. Исходник разбивается на два потока: размытый фон на весь кадр
         и основной кадр, вписанный по ширине.
      2. Оба накладываются друг на друга, лишнее обрезается по центру.
    """
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=20[bg];"
        f"[0:v]scale={width}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"format=yuv420p[v]"
    )


async def make_vertical(
    src: Path,
    dest: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    duration: int | None = None,
) -> Path:
    """
    Конвертирует видео в вертикальный формат 9:16.

    duration — максимальная длительность результата в секундах (обрезка -t).
    Если ffmpeg недоступен — возвращает исходный файл (graceful degradation),
    чтобы бот всё же отправил клип пользователю.
    """
    if not _ffmpeg_available():
        log.warning("ffmpeg не найден — отправляю исходный клип без обрезки")
        return src

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-i", str(src),
    ]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += [
        "-filter_complex", build_vertical_filter(width, height),
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    log.info("Запуск ffmpeg для вертикального формата: %s", dest.name)
    try:
        await _run(cmd)
    except FileNotFoundError as exc:  # pragma: no cover - среда без ffmpeg
        log.warning("ffmpeg недоступен (%s) — возвращаю исходное видео", exc)
        return src

    if not dest.exists() or dest.stat().st_size == 0:
        log.warning("ffmpeg не создал корректный файл — возвращаю исходник")
        return src
    log.info("Вертикальное видео готово: %s", dest)
    return dest
