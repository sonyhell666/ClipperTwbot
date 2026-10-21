#!/usr/bin/env bash
# ==========================================================================
# setup.sh — установка и запуск Telegram-бота Twitch-клипов на Ubuntu 22.04
#
# Запуск на чистом сервере (от root или через sudo):
#     sudo bash setup.sh
#
# Скрипт:
#   1. обновляет пакеты;
#   2. устанавливает Docker, Docker Compose plugin и ffmpeg;
#   3. создаёт /opt/twitchclipbot и копирует туда файлы проекта;
#   4. проверяет наличие .env (создаёт из .env.example, если нет);
#   5. собирает образ и запускает контейнер через docker compose.
# ==========================================================================
set -euo pipefail

PROJECT_DIR="/opt/twitchclipbot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf "\033[1;34m[setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn ]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[error]\033[0m %s\n" "$*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then
    err "Запусти скрипт от root или через sudo: sudo bash setup.sh"
    exit 1
fi

log "1/6 Обновляю индекс пакетов…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

log "2/6 Устанавливаю базовые утилиты (curl, ca-certificates, ffmpeg)…"
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release ffmpeg git

if command -v docker >/dev/null 2>&1; then
    log "Docker уже установлен: $(docker --version)"
else
    log "Устанавливаю Docker…"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi

log "3/6 Проверяю docker compose…"
if ! docker compose version >/dev/null 2>&1; then
    err "docker compose plugin недоступен. Установи его вручную."
    exit 1
fi
systemctl enable --now docker

log "4/6 Создаю $PROJECT_DIR и копирую файлы проекта…"
mkdir -p "$PROJECT_DIR"
# Копируем содержимое каталога скрипта (кроме .git, data, .env)
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude '.git' \
        --exclude 'data' \
        --exclude '.env' \
        --exclude '__pycache__' \
        "$SCRIPT_DIR/" "$PROJECT_DIR/"
else
    # fallback: cp без rsync
    find "$SCRIPT_DIR" -mindepth 1 -maxdepth 1 \
        ! -name '.git' ! -name 'data' ! -name '.env' \
        -exec cp -r {} "$PROJECT_DIR/" \;
fi
mkdir -p "$PROJECT_DIR/data/clips"

log "5/6 Проверяю .env…"
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    if [[ -f "$PROJECT_DIR/.env.example" ]]; then
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        warn "Создан $PROJECT_DIR/.env из .env.example."
        warn "ОБЯЗАТЕЛЬНО отредактируй его: nano $PROJECT_DIR/.env"
        warn "Заполни BOT_TOKEN, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, ADMIN_ID."
    else
        err ".env.example не найден — не могу создать .env"
        exit 1
    fi
fi

# Проверяем, что плейсхолдеры заменены
if grep -q "your_twitch_client_id" "$PROJECT_DIR/.env"; then
    warn "В .env остались значения-заглушки. Заполни их перед запуском:"
    warn "    nano $PROJECT_DIR/.env"
    read -r -p "Продолжить запуск всё равно? [y/N] " ans
    [[ "${ans,,}" == "y" ]] || exit 1
fi

log "6/6 Собираю и запускаю контейнер…"
cd "$PROJECT_DIR"
docker compose pull || true
docker compose up -d --build

log "Готово! Проверить статус:  docker compose -f $PROJECT_DIR/docker-compose.yml ps"
log "Логи:                     docker compose -f $PROJECT_DIR/docker-compose.yml logs -f"
