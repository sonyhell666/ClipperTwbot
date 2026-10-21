"""Telegram-хендлеры Twitch Clip Bot."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .chat_listener import ChatListener
from .clip_service import ClipResult, ClipService
from .config import Config
from .database import Database
from .twitch_oauth import exchange_code, fetch_login

log = logging.getLogger(__name__)
router = Router(name="main")


class LinkTwitch(StatesGroup):
    waiting_for_url = State()


class Settings(StatesGroup):
    waiting_duration = State()


def _oauth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Формирует OAuth URL с точным и корректно закодированным redirect_uri."""
    return "https://id.twitch.tv/oauth2/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "clips:edit",
            "state": state,
        }
    )


def _oauth_query(text: str) -> dict[str, str]:
    """Разбирает параметры из полной localhost-ссылки или query string."""
    value = text.strip()
    query = urlsplit(value).query if "://" in value else value.lstrip("?")
    parsed = parse_qs(query, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


BTN_LINK = "🔗 Подключить Twitch"
BTN_STATS = "📊 Моя статистика"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMIN = "🛠 Админка"


def start_keyboard(config: Config, telegram_id: int | None = None) -> ReplyKeyboardMarkup:
    """Постоянные кнопки меню под полем ввода; админка видна только ADMIN_ID."""
    rows = [
        [KeyboardButton(text=BTN_LINK)],
        [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_SETTINGS)],
    ]
    if telegram_id == config.admin_id:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def link_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔐 Авторизоваться в Twitch", url=url)]]
    )


def user_stats_keyboard(twitch_nick: str | None) -> InlineKeyboardMarkup:
    """Кнопки статистики обычного пользователя."""
    rows: list[list[InlineKeyboardButton]] = []
    if twitch_nick:
        rows.append(
            [InlineKeyboardButton(text="📈 По каналу", callback_data="my_channel_stats")]
        )
    rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(current: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора длительности клипа: пресеты + свой вариант."""
    durations = (10, 20, 30, 45, 60)
    row = [
        InlineKeyboardButton(
            text=f"{'✅ ' if d == current else ''}{d}с",
            callback_data=f"set_dur:{d}",
        )
        for d in durations
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            row,
            [InlineKeyboardButton(text="✏️ Своя длительность", callback_data="set_dur_custom")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_home")],
        ]
    )


def settings_text(duration: int) -> str:
    """Текст панели настроек с текущей длительностью клипа."""
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"⏱ Длительность клипа: <b>{duration} сек</b>\n\n"
        "Выбери пресет или введи своё значение (10–60 сек):"
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users:0")],
            [InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin_overview")],
            [InlineKeyboardButton(text="🏆 Топ каналов", callback_data="admin_top")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_home")],
        ]
    )


def admin_users_keyboard(page: int, total: int, per_page: int) -> InlineKeyboardMarkup:
    """Пагинация списка пользователей в админке."""
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="🔙 Админка", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clip_keyboard(clip_url: str, twitch_nick: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬇️ Скачать", callback_data="download_clip"),
                InlineKeyboardButton(text="🌐 Открыть на Twitch", url=clip_url),
            ],
            [InlineKeyboardButton(text="🔁 Повторить", callback_data=f"repeat_clip:{twitch_nick}")],
        ]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, config: Config) -> None:
    user = await db.get_user(message.from_user.id)
    if user and user.get("twitch_access_token"):
        text = (
            f"👋 С возвращением!\n\nПривязанный Twitch: <b>{user['twitch_nick']}</b>\n\n"
            "В чате доступна команда <code>!clip</code>"
        )
    else:
        text = (
            "👋 Привет! Я бот для создания клипов Twitch.\n\n"
            "Нажми «Подключить Twitch», подтверди доступ, затем скопируй адресную строку "
            "браузера и отправь её сюда."
        )
    await _send_panel(
        message, text, reply_markup=start_keyboard(config, message.from_user.id)
    )




async def _delete_quiet(message: Message) -> None:
    """Удаляет сообщение пользователя, чтобы не засорять чат."""
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass


async def _edit_quiet(callback: CallbackQuery, text: str, **kwargs) -> None:
    """Редактирует сообщение панели, молча игнорируя «не изменилось»."""
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


# Трекинг «панелей» меню: в каждом чате держим не больше одной —
# предыдущую удаляем, чтобы история не засорялась. Клипы и прочие
# сообщения сюда не попадают и никогда не удаляются.
_panel_msgs: dict[int, int] = {}


async def _drop_panel(bot: Bot, chat_id: int) -> None:
    """Удаляет предыдущую панель меню, если она ещё существует."""
    msg_id = _panel_msgs.pop(chat_id, None)
    if msg_id is None:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось удалить прошлую панель %s: %s", msg_id, exc)


async def _send_panel(message: Message, text: str, **kwargs) -> Message:
    """Отправляет новую панель меню, предварительно удалив предыдущую."""
    if message.bot is not None:
        await _drop_panel(message.bot, message.chat.id)
    sent = await message.answer(text, **kwargs)
    _panel_msgs[message.chat.id] = sent.message_id
    return sent


@router.message(F.text == BTN_LINK)
async def btn_link_twitch(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    """Reply-кнопка «Подключить Twitch» — тот же OAuth-флоу, что и inline."""
    await _delete_quiet(message)
    oauth_state = f"tg_{message.from_user.id}_{uuid.uuid4().hex[:8]}"
    await db.create_oauth_state(oauth_state, message.from_user.id)
    await state.set_state(LinkTwitch.waiting_for_url)
    url = _oauth_url(config.twitch_client_id, config.oauth_redirect_uri, oauth_state)
    await _send_panel(
        message,
        "Открой ссылку и подтверди доступ Twitch. После редиректа скопируй "
        "всю адресную строку с <code>code</code> и <code>state</code> и отправь сюда.",
        reply_markup=link_keyboard(url),
    )


@router.message(F.text == BTN_STATS)
async def btn_my_stats(message: Message, db: Database, config: Config) -> None:
    """Reply-кнопка «Моя статистика»."""
    await _delete_quiet(message)
    user = await db.get_user(message.from_user.id)
    if not user:
        await _send_panel(
            message,
            "Ты ещё не привязал Twitch-аккаунт — статистики пока нет.",
            reply_markup=start_keyboard(config, message.from_user.id),
        )
        return
    total = await db.count_clips(message.from_user.id)
    last = await db.user_clips(message.from_user.id, limit=1)
    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"🔗 Канал: <b>{user['twitch_nick']}</b>\n"
        f"🎬 Клипов создано через бота: <b>{total}</b>"
    )
    if last:
        text += f"\n🕒 Последний клип: {last[0]['created_at']}"
    await _send_panel(
        message,
        text,
        reply_markup=user_stats_keyboard(user["twitch_nick"]),
        disable_web_page_preview=True,
    )


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message, db: Database) -> None:
    """Reply-кнопка «Настройки» — показывает панель персональных настроек."""
    await _delete_quiet(message)
    duration = await db.get_clip_duration(message.from_user.id)
    await _send_panel(
        message,
        settings_text(duration),
        reply_markup=settings_keyboard(duration),
    )


@router.callback_query(F.data == "open_settings")
async def cb_open_settings(callback: CallbackQuery, db: Database) -> None:
    """Открывает панель настроек из инлайн-клавиатуры (редактирует текущую)."""
    duration = await db.get_clip_duration(callback.from_user.id)
    await callback.answer()
    await _edit_quiet(
        callback,
        settings_text(duration),
        reply_markup=settings_keyboard(duration),
    )


@router.callback_query(F.data.startswith("set_dur:"))
async def cb_set_duration(callback: CallbackQuery, db: Database) -> None:
    """Устанавливает длительность клипа по выбранному пресету."""
    try:
        duration = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Некорректное значение", show_alert=True)
        return
    duration = await db.set_clip_duration(callback.from_user.id, duration)
    await callback.answer(f"Установлено {duration} сек")
    await _edit_quiet(
        callback,
        settings_text(duration),
        reply_markup=settings_keyboard(duration),
    )


@router.callback_query(F.data == "set_dur_custom")
async def cb_set_duration_custom(callback: CallbackQuery, state: FSMContext) -> None:
    """Переводит пользователя в состояние ввода произвольной длительности."""
    await state.set_state(Settings.waiting_duration)
    await callback.answer()
    await _edit_quiet(
        callback,
        "✏️ Отправь желаемую длительность клипа в секундах — число от 10 до 60.\n\n"
        "Отмена: /cancel",
    )


@router.message(Settings.waiting_duration, F.text)
async def process_duration_input(
    message: Message, state: FSMContext, db: Database, config: Config
) -> None:
    """Принимает произвольную длительность клипа (10–60) и сохраняет её."""
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Нужно целое число от 10 до 60. Попробуй ещё раз.")
        return
    if not 10 <= value <= 60:
        await message.answer("⚠️ Длительность должна быть от 10 до 60 секунд.")
        return
    duration = await db.set_clip_duration(message.from_user.id, value)
    await state.clear()
    await _delete_quiet(message)
    await _send_panel(
        message,
        f"✅ Длительность клипа установлена: <b>{duration} сек</b>",
        reply_markup=settings_keyboard(duration),
    )


@router.message(F.text == BTN_ADMIN)
async def btn_admin_panel(message: Message, config: Config) -> None:
    """Reply-кнопка «Админка» (видна только админу)."""
    await _delete_quiet(message)
    if message.from_user.id != config.admin_id:
        return
    await _send_panel(
        message,
        "🛠 <b>Админ-панель</b>\n\nВыбери раздел:",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "link_twitch")
async def cb_link_twitch(
    callback: CallbackQuery, state: FSMContext, db: Database, config: Config
) -> None:
    """Создаёт одноразовый state и выдаёт персональную OAuth-ссылку."""
    oauth_state = f"tg_{callback.from_user.id}_{uuid.uuid4().hex[:8]}"
    await db.create_oauth_state(oauth_state, callback.from_user.id)
    await state.set_state(LinkTwitch.waiting_for_url)
    await callback.answer()
    if callback.message:
        url = _oauth_url(config.twitch_client_id, config.oauth_redirect_uri, oauth_state)
        await callback.message.answer(
            "Открой ссылку и подтверди доступ Twitch. После редиректа скопируй "
            "всю адресную строку с <code>code</code> и <code>state</code> и отправь сюда.",
            reply_markup=link_keyboard(url),
        )


@router.message(LinkTwitch.waiting_for_url, F.text)
async def process_oauth_url(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    listener: ChatListener,
) -> None:
    params = _oauth_query(message.text or "")
    if params.get("error"):
        await message.answer(
            "❌ Twitch вернул <code>redirect_mismatch</code>. Добавь ровно "
            "<code>http://localhost</code> в OAuth Redirect URLs приложения Twitch и повтори /start."
        )
        return
    code, oauth_state = params.get("code"), params.get("state")
    if not code or not oauth_state:
        await message.answer(
            "⚠️ Не найдены <code>code</code> и <code>state</code>. Отправь полную адресную строку браузера."
        )
        return
    telegram_id = await db.consume_oauth_state(oauth_state)
    if telegram_id != message.from_user.id:
        await message.answer(
            "⚠️ State устарел или относится к другому пользователю. Начни заново: /start"
        )
        return
    try:
        token_data = await exchange_code(
            code,
            config.twitch_client_id,
            config.twitch_client_secret,
            config.oauth_redirect_uri,
        )
        login, twitch_user_id = await fetch_login(
            token_data["access_token"], config.twitch_client_id
        )
        expires_in = int(token_data.get("expires_in", 14400))
        expires_at = str(time.time() + expires_in)
        await db.update_user_twitch_auth(
            message.from_user.id,
            twitch_login=login,
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            twitch_user_id=twitch_user_id,
            token_expires_at=expires_at,
        )
        listener.subscribe(login)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка Twitch OAuth для telegram_id=%s", message.from_user.id)
        await message.answer(
            f"❌ Не удалось завершить авторизацию: {exc}\n"
            "Получить новую ссылку можно через /start."
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Twitch <b>{login}</b> подключён!\n\n"
        "Теперь в чате доступна команда <code>!clip</code>",
        reply_markup=start_keyboard(config, message.from_user.id),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, config: Config) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять 🙂")
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=start_keyboard(config, message.from_user.id))


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database, config: Config) -> None:
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Ты ещё не привязал Twitch-аккаунт.", reply_markup=start_keyboard(config, message.from_user.id)
        )
        return
    clips = await db.user_clips(message.from_user.id, limit=5)
    lines = [f"🔗 Twitch: <b>{user['twitch_nick']}</b>", "", "Последние клипы:"]
    lines.extend(f"• <a href=\"{c['clip_url']}\">{c['created_at']}</a>" for c in clips)
    if not clips:
        lines.append("— пока нет —")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("unlink"))
async def cmd_unlink(
    message: Message, db: Database, config: Config, listener: ChatListener
) -> None:
    user = await db.get_user(message.from_user.id)
    if user:
        listener.unsubscribe(user["twitch_nick"])
    await db.delete_user(message.from_user.id)
    await message.answer("Twitch отвязан.", reply_markup=start_keyboard(config, message.from_user.id))



# ---------- Статистика пользователя ----------


@router.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: CallbackQuery, db: Database, config: Config) -> None:
    """Личная статистика: привязанный канал и число клипов."""
    user = await db.get_user(callback.from_user.id)
    clips_total = await db.count_clips(callback.from_user.id)
    last = await db.user_clips(callback.from_user.id, limit=1)
    last_clip = last[0] if last else None

    if not user:
        await callback.answer()
        if callback.message:
            await _edit_quiet(
                callback, "Ты ещё не привязал Twitch-аккаунт — статистики пока нет."
            )
        return

    text = (
        f"📊 <b>Твоя статистика</b>\n\n"
        f"🔗 Канал: <b>{user['twitch_nick']}</b>\n"
        f"🎬 Клипов создано через бота: <b>{clips_total}</b>\n"
    )
    if last_clip:
        text += (
            f"🕒 Последний клип: {last_clip['created_at']}\n"
            f"   <a href=\"{last_clip['clip_url']}\">открыть</a>\n"
        )
    await callback.answer()
    if callback.message:
        await _edit_quiet(
            callback,
            text,
            reply_markup=user_stats_keyboard(user["twitch_nick"]),
            disable_web_page_preview=True,
        )


@router.callback_query(F.data == "my_channel_stats")
async def cb_my_channel_stats(callback: CallbackQuery, db: Database) -> None:
    """Статистика по привязанному каналу: всего клипов + последние 5."""
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала привяжи Twitch-аккаунт", show_alert=True)
        return
    nick = user["twitch_nick"]
    clips = await db.user_clips(callback.from_user.id, limit=5)
    total = await db.count_clips(callback.from_user.id)
    channel_rank = await db.clips_per_channel(limit=100)
    rank = next(
        (i + 1 for i, c in enumerate(channel_rank) if c["twitch_nick"] == nick),
        None,
    )
    lines = [f"📈 <b>Канал {nick}</b>", f"🎬 Всего клипов: <b>{total}</b>"]
    if rank:
        lines.append(f"🏆 Место в рейтинге бота: <b>#{rank}</b>")
    if clips:
        lines += ["", "Последние клипы:"]
        lines += [
            f"• <a href=\"{c['clip_url']}\">{c['created_at']}</a>" for c in clips
        ]
    await callback.answer()
    if callback.message:
        await _edit_quiet(callback, "\n".join(lines), disable_web_page_preview=True)


# ---------- Админ-панель ----------


def _is_admin(callback: CallbackQuery, config: Config) -> bool:
    return callback.from_user.id == config.admin_id


@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery, config: Config) -> None:
    if not _is_admin(callback, config):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _edit_quiet(
            callback, "🛠 <b>Админ-панель</b>\n\nВыбери раздел:", reply_markup=admin_keyboard()
        )


@router.callback_query(F.data == "admin_overview")
async def cb_admin_overview(callback: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(callback, config):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    users_total = await db.count_users()
    clips_total = await db.count_clips_total()
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    clips_week = await db.count_clips_since(week_ago)
    await callback.answer()
    if callback.message:
        await _edit_quiet(
            callback,
            "📊 <b>Общая статистика</b>\n\n"
            f"👥 Пользователей привязано: <b>{users_total}</b>\n"
            f"🎬 Клипов всего: <b>{clips_total}</b>\n"
            f"🗓 Клипов за 7 дней: <b>{clips_week}</b>",
            reply_markup=admin_keyboard(),
        )


@router.callback_query(F.data == "admin_top")
async def cb_admin_top(callback: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(callback, config):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    top = await db.clips_per_channel(limit=10)
    lines = ["🏆 <b>Топ каналов по клипам</b>", ""]
    if top:
        medals = ["🥇", "🥈", "🥉"]
        for i, c in enumerate(top):
            mark = medals[i] if i < 3 else f"{i + 1}."
            lines.append(
                f"{mark} <b>{c['twitch_nick']}</b> — {c['clip_count']} клип(ов)"
                f" (посл.: {c['last_clip_at']})"
            )
    else:
        lines.append("Пока ни одного клипа не создано.")
    await callback.answer()
    if callback.message:
        await _edit_quiet(callback, "\n".join(lines), reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin_users:"))
async def cb_admin_users(callback: CallbackQuery, db: Database, config: Config) -> None:
    if not _is_admin(callback, config):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        page = 0
    per_page = 8
    rows = await db.users_with_clip_counts()
    total = len(rows)
    chunk = rows[page * per_page : (page + 1) * per_page]

    lines = [f"👥 <b>Пользователи</b> ({total})", ""]
    for u in chunk:
        last = u["last_clip_at"] or "—"
        lines.append(
            f"• <b>{u['twitch_nick']}</b> — {u['clip_count']} клип(ов)\n"
            f"  tg_id: <code>{u['telegram_id']}</code>, с {u['created_at']}, посл. клип: {last}"
        )
    if not chunk:
        lines.append("Пусто.")

    await callback.answer()
    if callback.message:
        await _edit_quiet(
            callback, "\n".join(lines), reply_markup=admin_users_keyboard(page, total, per_page)
        )


@router.callback_query(F.data == "back_home")
async def cb_back_home(callback: CallbackQuery, config: Config) -> None:
    """Возврат на главный экран."""
    await callback.answer()
    if callback.message:
        await _edit_quiet(callback, "🏠 Главное меню:")


@router.message(Command("admin"))
async def cmd_admin(message: Message, config: Config) -> None:
    if message.from_user.id != config.admin_id:
        await message.answer("⛔ Доступ запрещён")
        return
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\nВыбери раздел:",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "download_clip")
async def cb_download(callback: CallbackQuery) -> None:
    await callback.answer(
        "Нажми на видео и выбери «Сохранить» — файл уже доступен.", show_alert=True
    )


@router.callback_query(F.data.startswith("repeat_clip:"))
async def cb_repeat(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    clip_service: ClipService,
    http_session: aiohttp.ClientSession,
) -> None:
    nick = callback.data.split(":", 1)[1]
    await callback.answer("Создаю новый клип…")
    status_msg = None
    if callback.message:
        await bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_VIDEO)
        status_msg = await callback.message.answer(
            f"⏳ Создаю клип для <b>{nick}</b>, подожди 15–25 секунд…"
        )
    result = await clip_service.create_clip_for_user(
        callback.from_user.id, nick, http_session
    )
    await send_clip_result(
        bot,
        callback.from_user.id,
        result,
        status_msg_id=status_msg.message_id if status_msg else None,
        db=db,
    )


async def send_clip_result(
    bot: Bot,
    user_id: int,
    result: ClipResult,
    *,
    status_msg_id: int | None = None,
    db: Database | None = None,
) -> None:
    """Отправляет результат клипа пользователю."""
    if status_msg_id is not None:
        try:
            await bot.delete_message(user_id, status_msg_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось удалить статусное сообщение: %s", exc)
    if not result.success:
        text = f"❌ Не удалось сделать клип для <b>{result.twitch_nick}</b>."
        if result.error:
            text += f"\n\nПричина: {result.error}"
        if result.clip_url:
            text += f"\n\n🔗 <a href='{result.clip_url}'>Ссылка на клип</a>"
        await bot.send_message(user_id, text, disable_web_page_preview=False)
        return
    caption = (
        f"🎬 Новый клип для <b>{result.twitch_nick}</b>\n"
        f"🔗 <a href='{result.clip_url}'>Открыть на Twitch</a>"
    )
    keyboard = clip_keyboard(result.clip_url, result.twitch_nick)
    if result.video_path is None:
        await bot.send_message(user_id, caption, reply_markup=keyboard)
        return
    try:
        await bot.send_video(
            user_id,
            FSInputFile(result.video_path),
            caption=caption,
            reply_markup=keyboard,
            supports_streaming=True,
            width=1080,
            height=1920,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("send_video не удался, пробую документом: %s", exc)
        try:
            await bot.send_document(
                user_id, FSInputFile(result.video_path), caption=caption, reply_markup=keyboard
            )
        except Exception as exc2:  # noqa: BLE001
            log.error("Не удалось отправить видео: %s", exc2)
            await bot.send_message(user_id, f"⚠️ Видео не отправилось. {caption}", reply_markup=keyboard)


async def notify_admin(bot: Bot, admin_id: int, result: ClipResult) -> None:
    """Отправляет информацию о результате клипа администратору."""
    if not admin_id:
        return
    try:
        if result.success and result.video_path is not None:
            await bot.send_video(
                admin_id,
                FSInputFile(result.video_path),
                caption=(
                    f"🎬 [ADMIN] Клип от {result.twitch_nick}\n"
                    f"user_id: {result.user_id}\n🔗 {result.clip_url}"
                ),
                supports_streaming=True,
            )
        else:
            text = (
                f"⚠️ [ADMIN] Клип для <b>{result.twitch_nick}</b> не создан.\n"
                f"user_id: {result.user_id}\nОшибка: {result.error or 'неизвестно'}"
            )
            if result.clip_url:
                text += f"\n🔗 {result.clip_url}"
            await bot.send_message(admin_id, text)
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось уведомить админа: %s", exc)

