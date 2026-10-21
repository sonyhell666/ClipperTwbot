"""
Общие фикстуры pytest для тестов бота.
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from bot.database import Database


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """Свежая БД во временной папке для каждого теста."""
    database = Database(tmp_path / "test.sqlite3")
    await database.connect()
    yield database
    await database.close()
