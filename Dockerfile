# Dockerfile для Telegram-бота Twitch-клипов
# Образ: python:3.12-slim + ffmpeg (нужен для обрезки клипов в 9:16)

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# Системные зависимости: ffmpeg для обработки видео, curl для healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — так кэш Docker слоёв работает эффективнее
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код приложения
COPY bot/ ./bot/

# Каталог для SQLite и временных клипов (перекрывается volume'ом)
RUN mkdir -p /app/data/clips

# Запуск бота как модуля
CMD ["python", "-m", "bot.main"]
