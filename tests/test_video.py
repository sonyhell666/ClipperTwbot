"""Тесты логики вертикального формата (без реального ffmpeg)."""

from __future__ import annotations

import pytest

from bot.video import build_vertical_filter, make_vertical


@pytest.mark.asyncio
async def test_build_vertical_filter_contains_dimensions() -> None:
    f = build_vertical_filter(1080, 1920)
    assert "1080:1920" in f
    assert "overlay" in f
    assert "format=yuv420p" in f


@pytest.mark.asyncio
async def test_make_vertical_returns_src_without_ffmpeg(monkeypatch, tmp_path) -> None:
    """Если ffmpeg нет — возвращаем исходник без падения."""
    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake")
    monkeypatch.setattr("bot.video._ffmpeg_available", lambda: False)
    result = await make_vertical(src, tmp_path / "out.mp4")
    assert result == src
